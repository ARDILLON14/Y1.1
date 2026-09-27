from datetime import timedelta

import pytest

from copytrader.config.models import AppConfig, ExitsSection, SizingSection
from copytrader.core.types import ExitMode, OperatingLevel
from copytrader.positions.exits import PositionView, evaluate_exit, source_sell_fraction
from copytrader.risk.limits import EffectiveLimits
from copytrader.risk.sizing import SizingInput, compute_size, estimate_slippage_pct
from tests.helpers import T0


def _sizing(**kw):
    base = dict(
        sizing_capital_usd=1000,
        max_risk_per_trade_pct=1.0,
        stop_distance_pct=20,
        wallet_score=90,
        min_score=55,
        hourly_volatility=None,
        liquidity_usd=None,
        est_slippage_pct=None,
        max_slippage_pct=3,
        is_high_risk=False,
        same_category_positions=0,
        max_trade_usd=100,
        min_trade_usd=10,
        hard_cap_usd=250,
        total_capacity_usd=500,
        token_capacity_usd=100,
        wallet_risk_capacity_usd=30,
        high_risk_capacity_usd=100,
    )
    base.update(kw)
    return SizingInput(**base)


# ------------------------------------------------------------------ sizing
def test_risk_based_size():
    r = compute_size(_sizing(), SizingSection())
    assert r.size_usd == pytest.approx(50.0)  # 1000 × 1% / 20%
    assert r.rejected_reason is None


def test_size_never_depends_on_source_amount_and_is_capped():
    r = compute_size(
        _sizing(
            sizing_capital_usd=1e9,
            hard_cap_usd=250,
            max_trade_usd=100_000,
            total_capacity_usd=1e9,
            token_capacity_usd=1e9,
            wallet_risk_capacity_usd=1e9,
        ),
        SizingSection(),
    )
    assert r.size_usd == 250 and r.limited_by == "hard_cap"


def test_confidence_volatility_slippage_and_correlation_reduce_size():
    cfg = SizingSection()
    full = compute_size(_sizing(), cfg).size_usd
    assert compute_size(_sizing(wallet_score=55), cfg).size_usd == pytest.approx(full * cfg.confidence_min_mult)
    assert compute_size(_sizing(hourly_volatility=0.5), cfg).size_usd < full
    assert compute_size(_sizing(est_slippage_pct=2.5), cfg).size_usd < full
    assert compute_size(_sizing(same_category_positions=2), cfg).size_usd == pytest.approx(full / 1.5)
    assert compute_size(_sizing(is_high_risk=True), cfg).size_usd == pytest.approx(full * 0.5)


def test_liquidity_and_capacity_caps_and_minimum():
    cfg = SizingSection()
    r = compute_size(_sizing(liquidity_usd=2000), cfg)  # 1 % of pool = $20
    assert r.size_usd == pytest.approx(20) and r.limited_by == "liquidity"
    r = compute_size(_sizing(total_capacity_usd=5), cfg)
    assert r.rejected_reason and "mínimo" in r.rejected_reason
    r = compute_size(_sizing(wallet_risk_capacity_usd=2), cfg)  # 2 USD at risk / 20 % stop = 10 USD
    assert r.size_usd == pytest.approx(10)


def test_slippage_estimate():
    assert estimate_slippage_pct(1000, 200_000) == pytest.approx(100 * 1000 / 101_000)
    assert estimate_slippage_pct(10, None) is None


# ------------------------------------------------------------------ limits
def test_level4_caps_are_stricter():
    cfg = AppConfig.model_validate({"risk": {"capital_usd": 10_000, "max_trade_usd": 1000, "max_open_positions": 20}})
    normal = EffectiveLimits.from_config(cfg, OperatingLevel.LIVE)
    small = EffectiveLimits.from_config(cfg, OperatingLevel.LIVE_SMALL)
    assert normal.max_trade_usd == 1000
    assert small.max_trade_usd == 20 and small.max_open_positions == 3
    assert small.max_daily_loss_pct <= 2.0
    assert small.hard_max_trade_usd <= 100


def test_hard_cap_is_fraction_of_capital():
    cfg = AppConfig.model_validate({"risk": {"capital_usd": 200, "max_trade_usd": 50, "min_trade_usd": 5}})
    assert EffectiveLimits.from_config(cfg, OperatingLevel.PAPER).hard_max_trade_usd == 50


# ------------------------------------------------------------------ exits
def _view(mode=ExitMode.PROTECTED, entry=1.0, peak=1.0, hit=()):
    return PositionView(entry_price_usd=entry, peak_price_usd=peak, opened_at=T0, exit_mode=mode, tp_levels_hit=hit)


def test_stop_loss_and_emergency():
    cfg = ExitsSection()
    assert evaluate_exit(_view(), 0.79, T0, cfg).trigger == "stop_loss"
    assert evaluate_exit(_view(ExitMode.MIRROR), 0.79, T0, cfg) is None  # mirror ignores our SL
    assert evaluate_exit(_view(ExitMode.MIRROR), 0.49, T0, cfg).trigger == "emergency_stop"


def test_take_profit_ladder_in_order():
    cfg = ExitsSection()
    d1 = evaluate_exit(_view(), 1.6, T0, cfg)
    assert d1.trigger == "take_profit_1" and d1.fraction == 0.5 and d1.tp_level == 0
    # even at +200 %, TP2 fires only after TP1 was hit
    assert evaluate_exit(_view(), 3.0, T0, cfg).tp_level == 0
    d2 = evaluate_exit(_view(hit=(0,), peak=3.0), 3.0, T0, cfg)
    assert d2.trigger == "take_profit_2" and d2.full


def test_trailing_stop():
    cfg = ExitsSection(trailing_stop_pct=15, trailing_activation_pct=20)
    assert evaluate_exit(_view(peak=1.3, hit=(0,)), 1.2, T0, cfg) is None  # -7.7 % from peak
    d = evaluate_exit(_view(peak=1.4, hit=(0,)), 1.15, T0, cfg)
    assert d.trigger == "trailing_stop"
    assert evaluate_exit(_view(peak=1.1), 0.94, T0, cfg) is None  # trailing not armed below +20 %


def test_max_hold():
    cfg = ExitsSection(max_hold_minutes=60)
    assert evaluate_exit(_view(), 1.01, T0 + timedelta(minutes=59), cfg) is None
    assert evaluate_exit(_view(), 1.01, T0 + timedelta(minutes=61), cfg).trigger == "max_hold"


def test_source_sell_fraction_by_mode():
    cfg = ExitsSection()
    assert source_sell_fraction(0.3, ExitMode.MIRROR, cfg) == pytest.approx(0.3)
    assert source_sell_fraction(0.95, ExitMode.PROTECTED, cfg) == 1.0
    assert source_sell_fraction(None, ExitMode.PROTECTED, cfg) == 1.0
    assert source_sell_fraction(0.5, ExitMode.SMART, cfg) is None
    closing = ExitsSection(close_on_source_sell=True)
    assert source_sell_fraction(0.1, ExitMode.SMART, closing) == 1.0
    assert source_sell_fraction(0.1, ExitMode.MIRROR, closing) == 1.0
