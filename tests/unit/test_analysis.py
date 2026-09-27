from datetime import timedelta

import pytest

from copytrader.analysis import stats
from copytrader.analysis.metrics import compute_metrics, forward_win_rates
from copytrader.analysis.reconstruction import reconstruct
from copytrader.analysis.regimes import RegimeClassifier
from copytrader.core.types import Side
from tests.helpers import T0, swap, trade, trades_series


# ------------------------------------------------------------------ stats
def test_wilson_lower_bound_penalises_small_samples():
    assert stats.wilson_lower_bound(3, 3) < 0.6  # 3/3 is not "100 %"
    big = stats.wilson_lower_bound(300, 400)
    assert 0.70 < big < 0.75


def test_mean_lower_bound():
    assert stats.mean_lower_bound([0.1]) == 0.0  # single observation proves nothing
    lb = stats.mean_lower_bound([0.1, 0.2, 0.15, 0.12, 0.18])
    assert 0 < lb < 0.15


def test_drawdown_and_streaks():
    frac, absolute = stats.max_drawdown([100, 120, 90, 130, 65])
    assert frac == pytest.approx(0.5) and absolute == pytest.approx(65)
    assert stats.max_drawdown([100, -50])[0] == 1.0  # capped at 100 %
    assert stats.max_streak([True, False, False, False, True], False) == 3


def test_two_proportion_detects_drop():
    z, p = stats.two_proportion_z(70, 100, 10, 30)
    assert p < 0.01
    _, p2 = stats.two_proportion_z(50, 100, 14, 30)
    assert p2 > 0.2


def test_weighted_effective_n():
    assert stats.effective_n([1, 1, 1, 1], 4) == 4
    assert stats.effective_n([1, 0.01, 0.01, 0.01], 4) < 1.1


# ---------------------------------------------------------- reconstruction
def test_reconstruct_round_trip_with_partial_sells():
    s = [
        swap("W", "A", Side.BUY, T0, 100, 100),
        swap("W", "A", Side.BUY, T0 + timedelta(minutes=10), 100, 150),  # avg cost 1.25
        swap("W", "A", Side.SELL, T0 + timedelta(minutes=20), 100, 200),
        swap("W", "A", Side.SELL, T0 + timedelta(minutes=30), 100, 50),
    ]
    r = reconstruct("W", s)
    assert len(r.closed) == 1
    t = r.closed[0]
    assert t.cost_usd == 250 and t.proceeds_usd == 250 and t.pnl_usd == 0
    assert t.holding_seconds == 1800 and t.n_buys == 2 and t.n_sells == 2
    assert r.peak_deployed_usd == 250


def test_reconstruct_unmatched_sell_is_excluded():
    r = reconstruct("W", [swap("W", "A", Side.SELL, T0, 10, 1000)])
    assert r.closed == [] and r.unmatched_sells == 1


def test_reconstruct_sell_more_than_bought_only_counts_matched_part():
    s = [swap("W", "A", Side.BUY, T0, 10, 10), swap("W", "A", Side.SELL, T0 + timedelta(hours=1), 20, 40)]
    r = reconstruct("W", s)
    assert r.closed[0].proceeds_usd == 20  # only 10 of the 20 tokens had a known cost
    assert r.unmatched_sells == 1


def test_reconstruct_open_lots_and_stale():
    s = [swap("W", "A", Side.BUY, T0, 10, 10), swap("W", "B", Side.BUY, T0 + timedelta(days=40), 5, 50)]
    r = reconstruct("W", s, stale_before=T0 + timedelta(days=30))
    lots = {lot.token_mint: lot for lot in r.open_lots}
    assert lots["A"].stale and not lots["B"].stale


def test_reconstruct_dust_closes_cycle():
    s = [swap("W", "A", Side.BUY, T0, 1000, 100), swap("W", "A", Side.SELL, T0 + timedelta(minutes=5), 995, 200)]
    r = reconstruct("W", s, dust_fraction=0.01)
    assert len(r.closed) == 1 and r.open_lots == []


# ------------------------------------------------------------------ metrics
def test_metrics_basic_values():
    trades = trades_series([0.5, -0.2, 0.3, -0.1, 0.4])
    m = compute_metrics(trades, window="all", now=T0 + timedelta(days=5))
    assert m.n_closed_trades == 5 and m.wins == 3 and m.win_rate == pytest.approx(0.6)
    assert m.realized_pnl_usd == pytest.approx(90.0)
    assert m.profit_factor == pytest.approx(120 / 30)
    assert m.roi_pct == pytest.approx(18.0)
    assert m.expectancy_pct == pytest.approx(18.0)
    assert m.max_consecutive_losses == 1
    assert m.win_rate_lb < m.win_rate
    assert m.profit_factor_shrunk < m.profit_factor  # shrunk towards 1


def test_metrics_concentration_and_outliers():
    trades = trades_series([-0.1] * 10 + [20.0])
    m = compute_metrics(trades, window="all", now=T0 + timedelta(days=10))
    assert m.concentration["top_trade_share"] == pytest.approx(1.0)
    assert m.outliers["pnl_without_top_trade"] < 0
    assert m.outliers["outlier_pnl_share"] == pytest.approx(1.0)


def test_metrics_breakdowns_and_regimes():
    trades = [trade(0.1, hold_minutes=2, regime="bull", category="cap:small"),
              trade(-0.1, hold_minutes=600, regime="extreme_down", category="cap:micro",
                    start=T0 + timedelta(days=1)),
              trade(0.2, hold_minutes=3000, regime="extreme_up", category="cap:micro", start=T0 + timedelta(days=2))]
    m = compute_metrics(trades, window="all", now=T0 + timedelta(days=5), min_replicable_hold_seconds=300)
    assert m.fast_vs_slow["fast"]["n"] == 1 and m.fast_vs_slow["slow"]["n"] == 2
    assert set(m.by_regime) == {"bull", "extreme_down", "extreme_up"}
    assert m.extreme_moves["n"] == 2
    assert m.by_category["cap:micro"]["n"] == 2
    assert m.replicable_frac == pytest.approx(2 / 3)
    assert len(m.by_holding) == 3


def test_metrics_weighted_decay_changes_win_rate():
    trades = trades_series([-0.1] * 10 + [0.1] * 10)
    weights = stats.decay_weights([20 - i for i in range(20)], half_life_days=3)
    m = compute_metrics(trades, window="decayed", now=T0, weights=weights)
    assert m.win_rate > 0.9  # recent wins dominate
    assert m.n_effective < 20


def test_empty_metrics_are_safe():
    m = compute_metrics([], window="all", now=T0)
    assert m.n_closed_trades == 0 and m.win_rate is None
    assert m.to_dict()["window"] == "all"


def test_forward_win_rates():
    buys = [("A", T0 + timedelta(minutes=i), 1.0) for i in range(10)]
    rates = forward_win_rates(buys, lambda mint, ts: 1.1, [5.0, 60.0], now=T0 + timedelta(days=1))
    assert rates == {"5m": 1.0, "1h": 1.0}


def test_regime_classifier_has_no_lookahead():
    series = [(T0 + timedelta(hours=h), 100.0 + (h if h <= 24 else 24 + 3 * (h - 24))) for h in range(0, 60)]
    rc = RegimeClassifier(series, trend_threshold_pct=3, extreme_threshold_pct=30)
    assert rc.regime(T0 + timedelta(hours=10)) is None  # no 24h history yet
    assert rc.regime(T0 + timedelta(hours=24)) == "bull"
    assert rc.regime(T0 + timedelta(hours=48)) == "extreme_up"
