"""Adaptive exit profile: volatility stop, wallet timing and take profits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from copytrader.config.loader import build_config
from copytrader.core.errors import ConfigError
from copytrader.core.types import ExitMode
from copytrader.positions.adaptive import build_exit_profile, effective_exits
from copytrader.positions.exits import PositionView, evaluate_exit

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def exits(**kw):
    return build_config({"exits": {"volatility_stop": True, "wallet_exit_profile": True, **kw}}).exits


def test_adaptive_profile_is_off_by_default():
    cfg = build_config({}).exits
    assert not cfg.volatility_stop and not cfg.wallet_exit_profile
    assert build_exit_profile(cfg, hourly_volatility=0.1, median_hold_minutes=20, median_win_pct=20).empty
    # only the liquidity exit is on: the others did not help in the simulated walk-forward
    assert cfg.liquidity_drop_exit_pct == 50.0 and cfg.wallet_sells_exit_min == 0


def test_stop_follows_the_expected_move_while_holding():
    cfg = exits()
    one_hour = build_exit_profile(cfg, hourly_volatility=0.10, median_hold_minutes=60, median_win_pct=None)
    assert one_hour.stop_loss_pct == pytest.approx(20.0)  # 2 sigmas x 10 %/h x sqrt(1 h)
    assert one_hour.trailing_stop_pct == pytest.approx(15.0)  # trailing scales with the stop (20/20)
    calm = build_exit_profile(cfg, hourly_volatility=0.02, median_hold_minutes=60, median_win_pct=None)
    assert calm.stop_loss_pct == 8.0  # floor: noise must not stop us out
    assert calm.trailing_stop_pct == pytest.approx(6.0)
    wild = build_exit_profile(cfg, hourly_volatility=0.10, median_hold_minutes=240, median_win_pct=None)
    assert wild.stop_loss_pct == 35.0 and "acotado" in wild.notes[0]  # 40 % capped
    # unknown holding time: one hour; unknown volatility: no adaptive stop
    assert build_exit_profile(cfg, hourly_volatility=0.1, median_hold_minutes=None, median_win_pct=None).stop_loss_pct
    blind = build_exit_profile(cfg, hourly_volatility=None, median_hold_minutes=60, median_win_pct=None)
    assert blind.stop_loss_pct is None and blind.trailing_stop_pct is None
    off = build_exit_profile(
        exits(volatility_stop=False), hourly_volatility=0.1, median_hold_minutes=60, median_win_pct=None
    )
    assert off.stop_loss_pct is None


def test_time_limit_follows_the_wallets_holding_time():
    cfg = exits()
    scalper = build_exit_profile(cfg, hourly_volatility=None, median_hold_minutes=20, median_win_pct=None)
    assert scalper.max_hold_minutes == 60  # 3 x 20 min
    assert (
        build_exit_profile(cfg, hourly_volatility=None, median_hold_minutes=5, median_win_pct=None).max_hold_minutes
        == 30
    )
    swing = build_exit_profile(cfg, hourly_volatility=None, median_hold_minutes=3000, median_win_pct=None)
    assert swing.max_hold_minutes == 4320  # capped at 3 days, beyond the global 24 h
    no_limit = exits(max_hold_minutes=None)
    assert build_exit_profile(no_limit, hourly_volatility=None, median_hold_minutes=20, median_win_pct=None).empty


def test_take_profits_scale_towards_the_wallets_typical_winner():
    cfg = exits()  # first level +50 %
    small = build_exit_profile(cfg, hourly_volatility=None, median_hold_minutes=None, median_win_pct=20)
    assert small.take_profit_scale == 0.5  # 20/50 = 0.4, floor 0.5
    big = build_exit_profile(cfg, hourly_volatility=None, median_hold_minutes=None, median_win_pct=300)
    assert big.take_profit_scale == 2.0
    same = build_exit_profile(cfg, hourly_volatility=None, median_hold_minutes=None, median_win_pct=50)
    assert same.take_profit_scale is None
    off = exits(profile_take_profit=False)
    assert build_exit_profile(off, hourly_volatility=None, median_hold_minutes=None, median_win_pct=20).empty


def test_effective_config_applies_the_stored_profile():
    cfg = exits()
    eff = effective_exits(
        cfg, {"stop_loss_pct": 12.0, "trailing_stop_pct": 9.0, "max_hold_minutes": 90, "take_profit_scale": 0.6}
    )
    assert (eff.stop_loss_pct, eff.trailing_stop_pct, eff.max_hold_minutes) == (12.0, 9.0, 90.0)
    assert [lvl.gain_pct for lvl in eff.take_profit_levels] == pytest.approx([30.0, 90.0])
    assert eff.emergency_stop_loss_pct == cfg.emergency_stop_loss_pct  # never adapted
    assert effective_exits(cfg, None) is cfg and effective_exits(cfg, {}) is cfg
    # the stop never goes past the emergency stop; disabled features stay disabled
    assert effective_exits(cfg, {"stop_loss_pct": 80.0}).stop_loss_pct == cfg.emergency_stop_loss_pct
    no_trailing = exits(trailing_stop_pct=None, max_hold_minutes=None)
    eff2 = effective_exits(no_trailing, {"trailing_stop_pct": 9.0, "max_hold_minutes": 90})
    assert eff2.trailing_stop_pct is None and eff2.max_hold_minutes is None


def test_adaptive_stop_and_time_drive_the_exit():
    cfg = exits()
    view = PositionView(1.0, 1.0, NOW - timedelta(minutes=70), ExitMode.PROTECTED)
    tight = effective_exits(cfg, {"stop_loss_pct": 10.0})
    assert evaluate_exit(view, 0.88, NOW - timedelta(minutes=69), cfg) is None  # -12 %: fine for the global 20 %
    assert evaluate_exit(view, 0.88, NOW - timedelta(minutes=69), tight).trigger == "stop_loss"
    timed = effective_exits(cfg, {"max_hold_minutes": 60})
    assert evaluate_exit(view, 1.01, NOW, timed).trigger == "max_hold"
    assert evaluate_exit(view, 1.01, NOW, cfg) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"volatility_stop_min_pct": 40, "volatility_stop_max_pct": 30},
        {"volatility_stop_max_pct": 60, "emergency_stop_loss_pct": 50},
        {"profile_min_hold_minutes": 500, "profile_max_hold_minutes": 100},
    ],
)
def test_invalid_adaptive_settings(bad):
    with pytest.raises(ConfigError):
        exits(**bad)
