"""What copying a wallet would return: latency, price impact and costs."""

from __future__ import annotations

from datetime import timedelta

import pytest

from copytrader.analysis.metrics import compute_metrics
from copytrader.analysis.reconstruction import reconstruct
from copytrader.analysis.replication import (
    ReplicationParams,
    build_params,
    copy_stats,
    replicated_return,
    typical_size_usd,
)
from copytrader.config.loader import build_config
from copytrader.config.models import StatusRulesSection
from copytrader.core.types import ListType, Side, WalletStatus
from copytrader.scoring.status import decide_status
from tests.helpers import T0, swap, trade

DEEP = 5_000_000.0  # liquidity where our size is negligible


def params(latency: float = 3.0, **kw: float) -> ReplicationParams:
    base = {"size_usd": 50.0, "fixed_cost_frac": 0.0, "slippage_frac": 0.0, "min_liquidity_usd": 50_000.0}
    base.update(kw)
    return ReplicationParams(latency_seconds=latency, **base)  # type: ignore[arg-type]


def test_slow_trades_in_deep_pools_replicate_almost_fully():
    t = trade(0.20, cost=100, hold_minutes=360, liquidity=DEEP)
    copied = replicated_return(t, params())
    assert copied == pytest.approx(0.20, abs=0.002)


def test_trades_shorter_than_our_latency_cannot_be_copied():
    fast = trade(0.05, cost=100, hold_minutes=2 / 60, liquidity=DEEP)  # held 2 s, we arrive after 3 s
    assert replicated_return(fast, params()) == pytest.approx(0.0, abs=0.002)  # we buy at their exit price
    quick = trade(0.05, cost=100, hold_minutes=0.5, liquidity=DEEP)  # 30 s: we lose 10 % of the move
    assert 0.03 < replicated_return(quick, params()) < 0.05


def test_a_whale_buying_into_a_thin_pool_is_not_copyable():
    whale = trade(0.30, cost=10_000, hold_minutes=120, liquidity=50_000)  # their buy moved the pool 40 %
    copied = replicated_return(whale, params())
    assert copied is not None and copied < 0


def test_unknown_liquidity_is_never_assumed_below_our_minimum():
    t = trade(0.10, cost=2_000, hold_minutes=120)
    at_min = replicated_return(t, params(min_liquidity_usd=50_000))
    with_token_liq = replicated_return(t, params(min_liquidity_usd=50_000), token_liquidity=2_000_000)
    assert at_min < with_token_liq


def test_costs_and_slippage_are_paid_on_every_copy():
    t = trade(0.0, cost=100, hold_minutes=360, liquidity=DEEP)
    copied = replicated_return(t, params(fixed_cost_frac=0.02, slippage_frac=0.005))
    assert copied == pytest.approx(-0.03, abs=0.002)


def test_trade_without_exit_price_is_skipped():
    t = trade(0.1)
    t.exit_price_usd = None
    assert replicated_return(t, params()) is None


def test_copy_stats():
    trades = [trade(0.1), trade(-0.05), trade(0.2)]
    stats = copy_stats(trades, [0.08, -0.07, None])
    assert stats.n == 2
    assert stats.expectancy_pct == pytest.approx(0.5)
    assert stats.win_rate == pytest.approx(0.5)
    assert stats.profit_factor == pytest.approx(0.08 / 0.07)
    assert stats.copy_cost_pct == pytest.approx(2.0)  # mean original 2.5 % vs copied 0.5 %


def test_build_params_uses_the_risk_based_size_and_cost_model():
    cfg = build_config({"risk": {"capital_usd": 1000, "max_risk_per_trade_pct": 1, "max_trade_usd": 100}})
    assert typical_size_usd(cfg) == pytest.approx(50.0)  # 1 % of 1000 at a 20 % stop
    p = build_params(cfg, latency_seconds=4, sol_price_usd=200)
    assert p.size_usd == 50 and p.latency_seconds == 4
    assert p.fixed_cost_frac == pytest.approx(0.402 / 50)  # ~0.002 SOL round trip at 200 USD/SOL
    fixed = build_config({"analysis": {"replication_size_usd": 25}})
    assert build_params(fixed, latency_seconds=1, sol_price_usd=200).size_usd == 25


def test_reconstruction_records_first_buy_and_average_exit():
    swaps = [
        swap("W", "M", Side.BUY, T0, 100, 100),
        swap("W", "M", Side.BUY, T0 + timedelta(minutes=1), 100, 120),
        swap("W", "M", Side.SELL, T0 + timedelta(minutes=5), 100, 150),
        swap("W", "M", Side.SELL, T0 + timedelta(minutes=9), 100, 170),
    ]
    t = reconstruct("W", swaps).closed[0]
    assert t.entry_value_usd == 100 and t.entry_price_usd == 1.0
    assert t.exit_price_usd == pytest.approx(1.6)


def _metrics(returns: list[float], hold_minutes: float):
    trades = [
        trade(r, hold_minutes=hold_minutes, liquidity=DEEP, start=T0 + timedelta(hours=i))
        for i, r in enumerate(returns)
    ]
    return compute_metrics(trades, window="all", now=T0 + timedelta(days=5), replication=params(fixed_cost_frac=0.02))


def test_metrics_and_status_flag_wallets_whose_edge_is_not_replicable():
    rules = StatusRulesSection(min_trades_active=10, min_score_active=0)
    scalper = _metrics([0.015] * 30, hold_minutes=0.2)  # +1.5 % per 12-second trade
    assert scalper.copy_n == 30 and scalper.copy_expectancy_pct < 0 < scalper.expectancy_pct
    decision = decide_status(list_type=ListType.NONE, score=80, metrics=scalper, flags=[], rules=rules)
    assert decision.status is WalletStatus.OBSERVE
    assert "no replicable" in decision.reasons[0] and "latencia ~3.0s" in decision.reasons[0]

    swing = _metrics([0.10] * 30, hold_minutes=240)
    assert swing.copy_expectancy_pct > 5
    assert decide_status(list_type=ListType.NONE, score=80, metrics=swing, flags=[], rules=rules).status is (
        WalletStatus.ACTIVE
    )
    disabled = StatusRulesSection(min_trades_active=10, min_score_active=0, min_copy_expectancy_pct=None)
    assert decide_status(list_type=ListType.NONE, score=80, metrics=scalper, flags=[], rules=disabled).status is (
        WalletStatus.ACTIVE
    )


def test_signal_age_limit_follows_the_wallets_holding_time():
    from copytrader.signals.engine import signal_age_limit

    cfg = build_config({"latency": {"max_signal_age_seconds": 20, "max_age_fraction_of_hold": 0.1}})
    limit, why = signal_age_limit(cfg, 60)  # scalper: 1 minute median hold
    assert limit == pytest.approx(6.0) and "holding mediano de 60s" in why
    assert signal_age_limit(cfg, 2 * 3600) == (20, "límite global")  # never above the global ceiling
    assert signal_age_limit(cfg, 5)[0] == pytest.approx(2.0)  # floor
    assert signal_age_limit(cfg, None) == (20, "límite global")  # unknown: global
    off = build_config({"latency": {"per_wallet_max_age": False}})
    assert signal_age_limit(off, 60)[1] == "límite global"
