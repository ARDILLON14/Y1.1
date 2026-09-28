"""Dynamic fee policy: urgency, size cap, Jito tips from the market and fees actually paid."""

from __future__ import annotations

import pytest

from copytrader.config.loader import build_config
from copytrader.core.errors import ConfigError
from copytrader.core.types import OrderPurpose
from copytrader.execution.costs import expected_priority_fee_lamports, priority_cap_lamports, swap_fee_lamports
from copytrader.execution.fees import FeePolicy, FeeTracker, JitoTipFloor

SOL = 200.0


def cfg(**execution):
    return build_config({"execution": execution})


def policy(c, tracker=None, tip_floor=None) -> FeePolicy:
    return FeePolicy(lambda: c, tracker, tip_floor)


def floor(**tips_sol) -> JitoTipFloor:
    f = JitoTipFloor(http=None, url="")  # type: ignore[arg-type]
    f.update([{f"landed_tips_{k[1:]}th_percentile": v for k, v in tips_sol.items()}])
    return f


def test_priority_cap_follows_the_trade_size_with_a_floor():
    c = cfg()
    assert priority_cap_lamports(c) == 1_000_000  # size unknown: absolute cap
    assert priority_cap_lamports(c, 20.0, SOL) == 500_000  # 0.5 % of 20 USD = 0.0005 SOL
    assert priority_cap_lamports(c, 1_000.0, SOL) == 1_000_000  # big trades stay at the absolute cap
    assert priority_cap_lamports(c, 0.2, SOL) == 10_000  # tiny trades keep a minimum priority
    assert priority_cap_lamports(cfg(priority_fee_max_trade_pct=None), 20.0, SOL) == 1_000_000


def test_expected_fee_prefers_observed_then_configured_then_market_then_cap():
    c = cfg()
    assert swap_fee_lamports(c, 20.0, SOL) == 5_000 + 500_000
    assert swap_fee_lamports(c, 20.0, SOL, observed=120_000) == 120_000
    # observed history never exceeds what the current caps allow
    assert swap_fee_lamports(c, 20.0, SOL, observed=900_000) == 505_000
    configured = cfg(expected_priority_fee_lamports=50_000)
    assert expected_priority_fee_lamports(configured, 20.0, SOL, market=300_000) == 50_000
    assert expected_priority_fee_lamports(c, 20.0, SOL, market=300_000) == 300_000
    assert expected_priority_fee_lamports(c, 20.0, SOL, market=900_000) == 500_000


def test_entries_and_routine_exits_are_size_capped_protective_exits_are_not():
    p = policy(cfg())
    entry = p.decide(purpose=OrderPurpose.ENTRY, notional_usd=20.0, sol_price=SOL)
    assert (entry.priority_level, entry.priority_max_lamports, entry.urgent) == ("veryHigh", 500_000, False)
    tp = p.decide(purpose=OrderPurpose.EXIT, notional_usd=20.0, sol_price=SOL, trigger="take_profit")
    assert (tp.priority_level, tp.priority_max_lamports, tp.urgent) == ("high", 500_000, False)
    for trigger in ("stop_loss", "emergency_stop", "trailing_stop", "kill_switch", "source_sell", "manual"):
        sl = p.decide(purpose=OrderPurpose.EXIT, notional_usd=20.0, sol_price=SOL, trigger=trigger)
        assert (sl.priority_level, sl.priority_max_lamports, sl.urgent) == ("veryHigh", 1_000_000, True), trigger
    # a routine exit that already failed once becomes urgent
    retry = p.decide(purpose=OrderPurpose.EXIT, notional_usd=20.0, sol_price=SOL, trigger="take_profit", attempt=2)
    assert retry.urgent and retry.priority_max_lamports == 1_000_000


def test_jito_tip_follows_landed_tips_within_limits():
    c = cfg(jito_tip_lamports=2_000_000, jito_tip_percentile=50)
    tips = floor(p25=0.00001, p50=0.0002, p75=0.001, p95=0.004, p99=0.01)
    p = policy(c, tip_floor=tips)
    small = p.decide(purpose=OrderPurpose.ENTRY, notional_usd=20.0, sol_price=SOL)
    assert small.priority_max_lamports == 0 and small.jito_tip_lamports == 200_000  # market p50, under the size cap
    assert small.source == "jito_p50"
    tiny = p.decide(purpose=OrderPurpose.ENTRY, notional_usd=2.0, sol_price=SOL)
    assert tiny.jito_tip_lamports == 50_000  # size cap (0.5 % of 2 USD)
    urgent = p.decide(purpose=OrderPurpose.EXIT, notional_usd=20.0, sol_price=SOL, trigger="stop_loss")
    assert urgent.jito_tip_lamports == 2_000_000  # protective exit: the configured maximum
    # the market tip also sets the expected cost in paper / cost filter
    assert p.expected_swap_fee_lamports(20.0, SOL) == 5_000 + 200_000
    # a market tip below Jito's minimum is raised to it
    low = policy(c, tip_floor=floor(p50=0.0000001))
    assert low.decide(purpose=OrderPurpose.ENTRY, notional_usd=20.0, sol_price=SOL).jito_tip_lamports == 1_000
    # unknown market: the (size-capped) maximum
    blind = policy(c).decide(purpose=OrderPurpose.ENTRY, notional_usd=20.0, sol_price=SOL)
    assert blind.jito_tip_lamports == 500_000 and blind.source == "jito_cap"


def test_tip_floor_parsing_and_staleness():
    f = JitoTipFloor(http=None, url="", max_age=60)  # type: ignore[arg-type]
    f.update([{"landed_tips_50th_percentile": 0.0003, "landed_tips_95th_percentile": "0.002", "junk": 1}], now=100.0)
    assert f.lamports(50, now=110.0) == 300_000 and f.lamports(95, now=110.0) == 2_000_000
    assert f.lamports(75, now=110.0) is None
    assert f.lamports(50, now=200.0) is None  # stale: not used
    f.update({"unexpected": True})
    assert f.lamports(50, now=110.0) == 300_000  # bad payloads keep the last good data


def test_tracker_needs_enough_samples():
    t = FeeTracker(window=5)
    t.load([10, 20])
    assert t.typical(3) is None
    t.record(30)
    t.record(0)  # ignored
    assert t.typical(3) == 20
    t.load([100] * 5)
    assert len(t) == 5 and t.typical(3) == 100
    p = policy(cfg(fee_min_samples=3), tracker=t)
    assert p.observed_fee_lamports() == 100
    assert p.expected_swap_fee_lamports(20.0, SOL) == 100


def test_modeled_fee_in_paper_mirrors_the_decision():
    p = policy(cfg(expected_priority_fee_lamports=200_000))
    entry = p.decide(purpose=OrderPurpose.ENTRY, notional_usd=20.0, sol_price=SOL)
    assert p.modeled_fee_lamports(entry, 20.0, SOL) == 5_000 + 200_000
    tiny = p.decide(purpose=OrderPurpose.ENTRY, notional_usd=5.0, sol_price=SOL)
    assert p.modeled_fee_lamports(tiny, 5.0, SOL) == 5_000 + 125_000  # size cap binds
    jito = policy(cfg(jito_tip_lamports=300_000))
    d = jito.decide(purpose=OrderPurpose.EXIT, notional_usd=20.0, sol_price=SOL, trigger="stop_loss")
    assert jito.modeled_fee_lamports(d, 20.0, SOL) == 5_000 + 300_000


@pytest.mark.parametrize(
    "bad",
    [
        {"send_via_jito": True},  # Jito without a tip
        {"jito_only": True, "jito_tip_lamports": 500},
        {"jito_tip_lamports": 5_000, "min_jito_tip_lamports": 10_000},
        {"priority_fee_max_lamports": 5_000, "min_priority_fee_lamports": 10_000},
    ],
)
def test_invalid_fee_settings_are_rejected(bad):
    with pytest.raises(ConfigError):
        cfg(**bad)
