"""Wallet performance metrics.

``compute_metrics`` is a pure function of closed trades (+ open lots). It is
evaluated on three windows by the analyzer:

* ``all``      — every trade, unweighted (what the dashboard shows);
* ``decayed``  — every trade, exponentially weighted by age (history that
                 "fades", used for the historical score);
* ``recent``   — only the last N trades (used to detect deterioration).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from copytrader.analysis import stats
from copytrader.analysis.regimes import EXTREME_REGIMES
from copytrader.analysis.replication import ReplicationParams, copy_stats, replicated_return
from copytrader.core.models import ClosedTrade, OpenLot


@dataclass
class WalletMetrics:
    window: str
    computed_at: datetime
    n_swaps: int = 0
    n_buys: int = 0
    n_sells: int = 0
    n_closed_trades: int = 0
    n_effective: float = 0.0
    n_open_positions: int = 0
    n_unmatched_sells: int = 0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    total_pnl_usd: float = 0.0
    invested_usd: float = 0.0
    roi_pct: float | None = None
    total_roi_pct: float | None = None
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    win_rate_lb: float | None = None
    avg_win_usd: float | None = None
    avg_loss_usd: float | None = None
    avg_win_pct: float | None = None
    avg_loss_pct: float | None = None
    payoff_ratio: float | None = None
    profit_factor: float | None = None
    profit_factor_shrunk: float | None = None
    expectancy_pct: float | None = None
    expectancy_lb_pct: float | None = None
    median_return_pct: float | None = None
    return_std_pct: float | None = None
    sharpe_per_trade: float | None = None
    sortino_per_trade: float | None = None
    best_trade_pct: float | None = None
    worst_trade_pct: float | None = None
    max_drawdown_pct: float | None = None
    max_drawdown_usd: float | None = None
    estimated_capital_usd: float | None = None
    avg_holding_minutes: float | None = None
    median_holding_minutes: float | None = None
    trades_per_day: float | None = None
    active_days: int = 0
    span_days: float | None = None
    first_trade_at: datetime | None = None
    last_trade_at: datetime | None = None
    days_since_last_trade: float | None = None
    avg_trade_size_usd: float | None = None
    median_trade_size_usd: float | None = None
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    profitable_days_frac: float | None = None
    profitable_weeks_frac: float | None = None
    daily_pnl_std_usd: float | None = None
    replicable_frac: float | None = None
    # Estimated result of COPYING the wallet (see analysis/replication.py)
    copy_n: int = 0
    copy_expectancy_pct: float | None = None
    copy_expectancy_lb_pct: float | None = None
    copy_win_rate: float | None = None
    copy_profit_factor: float | None = None
    copy_cost_pct: float | None = None
    replication: dict[str, Any] = field(default_factory=dict)
    period_pnl: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    by_token: list[dict[str, Any]] = field(default_factory=list)
    by_category: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_holding: dict[str, dict[str, Any]] = field(default_factory=dict)
    fast_vs_slow: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_regime: dict[str, dict[str, Any]] = field(default_factory=dict)
    extreme_moves: dict[str, Any] = field(default_factory=dict)
    forward_win_rates: dict[str, float] = field(default_factory=dict)
    concentration: dict[str, float | None] = field(default_factory=dict)
    outliers: dict[str, float | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        def conv(v: Any) -> Any:
            if isinstance(v, datetime):
                return v.isoformat()
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            if isinstance(v, dict):
                return {k: conv(x) for k, x in v.items()}
            if isinstance(v, list):
                return [conv(x) for x in v]
            return v

        return {k: conv(v) for k, v in asdict(self).items()}


def _bucket_stats(trades: Sequence[ClosedTrade]) -> dict[str, Any]:
    if not trades:
        return {"n": 0, "pnl_usd": 0.0, "win_rate": None, "avg_return_pct": None}
    wins = sum(1 for t in trades if t.is_win)
    return {
        "n": len(trades),
        "pnl_usd": round(sum(t.pnl_usd for t in trades), 2),
        "win_rate": wins / len(trades),
        "avg_return_pct": 100 * (stats.mean([t.return_frac for t in trades]) or 0.0),
    }


def _holding_label(minutes: float, edges: Sequence[float]) -> str:
    prev = 0.0
    for edge in edges:
        if minutes < edge:
            return f"{_fmt_minutes(prev)}-{_fmt_minutes(edge)}"
        prev = edge
    return f">{_fmt_minutes(prev)}"


def _fmt_minutes(m: float) -> str:
    if m == 0:
        return "0"
    if m < 60:
        return f"{m:g}m"
    if m < 1440:
        return f"{m / 60:g}h"
    return f"{m / 1440:g}d"


def compute_metrics(
    trades: Sequence[ClosedTrade],
    *,
    window: str,
    now: datetime,
    open_lots: Sequence[OpenLot] = (),
    weights: Sequence[float] | None = None,
    z: float = 1.645,
    pf_prior_trades: float = 10.0,
    holding_edges_minutes: Sequence[float] = (5.0, 60.0, 1440.0),
    fast_trade_max_minutes: float = 60.0,
    min_replicable_hold_seconds: float = 60.0,
    peak_deployed_usd: float = 0.0,
    n_swaps: int = 0,
    n_buys: int = 0,
    n_sells: int = 0,
    unmatched_sells: int = 0,
    swap_times: Sequence[datetime] = (),
    outlier_multiple: float = 10.0,
    forward_win_rates: dict[str, float] | None = None,
    replication: ReplicationParams | None = None,
    token_liquidity: Mapping[str, float | None] | None = None,
) -> WalletMetrics:
    m = WalletMetrics(
        window=window,
        computed_at=now,
        n_swaps=n_swaps,
        n_buys=n_buys,
        n_sells=n_sells,
        n_unmatched_sells=unmatched_sells,
    )
    trades = sorted(trades, key=lambda t: t.closed_at)
    if weights is not None and len(weights) != len(trades):
        raise ValueError("weights must align with trades")
    n = len(trades)
    m.n_closed_trades = n
    m.n_effective = stats.effective_n(weights, n)
    m.n_open_positions = sum(1 for lot in open_lots if not lot.stale)
    m.unrealized_pnl_usd = sum(lot.unrealized_pnl_usd or 0.0 for lot in open_lots if not lot.stale)
    m.forward_win_rates = dict(forward_win_rates or {})

    times = sorted(swap_times) or sorted([t.opened_at for t in trades] + [t.closed_at for t in trades])
    if times:
        m.first_trade_at, m.last_trade_at = times[0], times[-1]
        m.days_since_last_trade = (now - times[-1]).total_seconds() / 86400
        m.span_days = max((times[-1] - times[0]).total_seconds() / 86400, 1 / 24)
        m.active_days = len({t.date() for t in times})

    if n == 0:
        m.total_pnl_usd = m.unrealized_pnl_usd
        return m

    returns = [t.return_frac for t in trades]
    pnls = [t.pnl_usd for t in trades]
    w = list(weights) if weights is not None else None
    win_flags = [t.is_win for t in trades]
    m.realized_pnl_usd = sum(pnls)
    m.total_pnl_usd = m.realized_pnl_usd + m.unrealized_pnl_usd
    m.invested_usd = sum(t.cost_usd for t in trades)
    if m.invested_usd > 0:
        m.roi_pct = 100 * m.realized_pnl_usd / m.invested_usd
        open_cost = sum(lot.cost_usd for lot in open_lots if not lot.stale)
        m.total_roi_pct = 100 * m.total_pnl_usd / (m.invested_usd + open_cost)

    # --- win/loss statistics (weighted where weights are given)
    wsum = sum(w) if w else float(n)
    wins_w = sum(wi for wi, f in zip(w, win_flags, strict=True) if f) if w else float(sum(win_flags))
    m.wins = sum(win_flags)
    m.losses = n - m.wins
    m.win_rate = wins_w / wsum if wsum > 0 else None
    m.win_rate_lb = stats.wilson_lower_bound((m.win_rate or 0) * m.n_effective, m.n_effective, z)
    win_tr = [t for t in trades if t.is_win]
    loss_tr = [t for t in trades if not t.is_win]
    if win_tr:
        m.avg_win_usd = stats.mean([t.pnl_usd for t in win_tr])
        m.avg_win_pct = 100 * (stats.mean([t.return_frac for t in win_tr]) or 0.0)
    if loss_tr:
        m.avg_loss_usd = abs(stats.mean([t.pnl_usd for t in loss_tr]) or 0.0)
        m.avg_loss_pct = abs(100 * (stats.mean([t.return_frac for t in loss_tr]) or 0.0))
    if m.avg_win_pct is not None and m.avg_loss_pct:
        m.payoff_ratio = m.avg_win_pct / m.avg_loss_pct

    if w:
        gross_profit = sum(wi * p for wi, p in zip(w, pnls, strict=True) if p > 0)
        gross_loss = -sum(wi * p for wi, p in zip(w, pnls, strict=True) if p < 0)
    else:
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p < 0)
    m.profit_factor = stats.safe_ratio(gross_profit, gross_loss)
    # Shrink PF towards 1 by adding pseudo-trades of average absolute size on both sides.
    avg_abs = (gross_profit + gross_loss) / max(wsum, 1e-9)
    pseudo = pf_prior_trades * avg_abs / 2
    if gross_loss + pseudo > 0:
        m.profit_factor_shrunk = (gross_profit + pseudo) / (gross_loss + pseudo)

    m.expectancy_pct = 100 * (stats.mean(returns, w) or 0.0)
    lb = stats.mean_lower_bound(returns, z, w)
    m.expectancy_lb_pct = 100 * lb if lb is not None else None
    m.median_return_pct = 100 * (stats.median(returns) or 0.0)
    s = stats.std(returns, w)
    m.return_std_pct = 100 * s if s is not None else None
    m.sharpe_per_trade = stats.sharpe(returns)
    m.sortino_per_trade = stats.sortino(returns)
    m.best_trade_pct = 100 * max(returns)
    m.worst_trade_pct = 100 * min(returns)

    # --- drawdown on realized equity (capital estimated as peak deployed)
    capital = peak_deployed_usd or 5 * (stats.median([t.cost_usd for t in trades]) or 0.0)
    m.estimated_capital_usd = capital
    equity = [capital]
    running = capital
    for p in pnls:
        running += p
        equity.append(running)
    dd_frac, dd_abs = stats.max_drawdown(equity)
    m.max_drawdown_pct = 100 * dd_frac
    m.max_drawdown_usd = dd_abs

    holds = [t.holding_seconds / 60 for t in trades]
    m.avg_holding_minutes = stats.mean(holds)
    m.median_holding_minutes = stats.median(holds)
    if m.span_days:
        m.trades_per_day = n / m.span_days
    sizes = [t.cost_usd for t in trades]
    m.avg_trade_size_usd = stats.mean(sizes)
    m.median_trade_size_usd = stats.median(sizes)
    m.max_consecutive_losses = stats.max_streak(win_flags, False)
    m.max_consecutive_wins = stats.max_streak(win_flags, True)
    m.replicable_frac = sum(1 for t in trades if t.holding_seconds >= min_replicable_hold_seconds) / n
    if replication is not None:
        liq = token_liquidity or {}
        copied = [replicated_return(t, replication, liq.get(t.token_mint)) for t in trades]
        cs = copy_stats(trades, copied, weights=w, z=z)
        m.copy_n = cs.n
        m.copy_expectancy_pct = cs.expectancy_pct
        m.copy_expectancy_lb_pct = cs.expectancy_lb_pct
        m.copy_win_rate = cs.win_rate
        m.copy_profit_factor = cs.profit_factor
        m.copy_cost_pct = cs.copy_cost_pct
        m.replication = replication.describe()

    # --- consistency by period
    daily: dict[str, list[float]] = defaultdict(list)
    weekly: dict[str, list[float]] = defaultdict(list)
    monthly: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        d = t.closed_at
        daily[d.strftime("%Y-%m-%d")].append(t.pnl_usd)
        iso = d.isocalendar()
        weekly[f"{iso.year}-W{iso.week:02d}"].append(t.pnl_usd)
        monthly[d.strftime("%Y-%m")].append(t.pnl_usd)

    def series(groups: dict[str, list[float]], limit: int) -> list[dict[str, Any]]:
        keys = sorted(groups)[-limit:]
        return [{"period": k, "pnl_usd": round(sum(groups[k]), 2), "n": len(groups[k])} for k in keys]

    m.period_pnl = {"daily": series(daily, 90), "weekly": series(weekly, 52), "monthly": series(monthly, 24)}
    day_totals = [sum(v) for v in daily.values()]
    week_totals = [sum(v) for v in weekly.values()]
    m.profitable_days_frac = sum(1 for v in day_totals if v > 0) / len(day_totals)
    m.profitable_weeks_frac = sum(1 for v in week_totals if v > 0) / len(week_totals)
    m.daily_pnl_std_usd = stats.std(day_totals)

    # --- breakdowns
    by_token: dict[str, list[ClosedTrade]] = defaultdict(list)
    by_cat: dict[str, list[ClosedTrade]] = defaultdict(list)
    by_hold: dict[str, list[ClosedTrade]] = defaultdict(list)
    by_reg: dict[str, list[ClosedTrade]] = defaultdict(list)
    for t in trades:
        by_token[t.token_mint].append(t)
        by_cat[t.category or "unknown"].append(t)
        by_hold[_holding_label(t.holding_seconds / 60, holding_edges_minutes)].append(t)
        if t.regime:
            by_reg[t.regime].append(t)
    token_rows = [{"mint": mint, **_bucket_stats(ts)} for mint, ts in by_token.items()]
    token_rows.sort(key=lambda r: abs(r["pnl_usd"]), reverse=True)
    m.by_token = token_rows[:25]
    m.by_category = {k: _bucket_stats(v) for k, v in sorted(by_cat.items())}
    m.by_holding = {k: _bucket_stats(v) for k, v in by_hold.items()}
    fast = [t for t in trades if t.holding_seconds / 60 <= fast_trade_max_minutes]
    slow = [t for t in trades if t.holding_seconds / 60 > fast_trade_max_minutes]
    m.fast_vs_slow = {"fast": _bucket_stats(fast), "slow": _bucket_stats(slow)}
    m.by_regime = {k: _bucket_stats(v) for k, v in sorted(by_reg.items())}
    extreme = [t for t in trades if t.regime in EXTREME_REGIMES]
    m.extreme_moves = _bucket_stats(extreme)

    # --- concentration & outlier dependence
    positive = sorted((p for p in pnls if p > 0), reverse=True)
    total_pos = sum(positive)
    token_pos = [sum(t.pnl_usd for t in ts) for ts in by_token.values()]
    m.concentration = {
        "top_trade_share": positive[0] / total_pos if total_pos > 0 else None,
        "top3_share": sum(positive[:3]) / total_pos if total_pos > 0 else None,
        "token_hhi": stats.hhi([max(0.0, v) for v in token_pos]),
        "largest_size_share": max(sizes) / sum(sizes) if sum(sizes) > 0 else None,
    }
    outlier_pnl = sum(t.pnl_usd for t in trades if t.return_frac >= outlier_multiple - 1)
    m.outliers = {
        "pnl_without_top_trade": m.realized_pnl_usd - (positive[0] if positive else 0.0),
        "n_outliers": float(sum(1 for t in trades if t.return_frac >= outlier_multiple - 1)),
        "outlier_pnl_share": outlier_pnl / total_pos if total_pos > 0 else None,
    }
    return m


def forward_win_rates(
    buys: Sequence[tuple[str, datetime, float]],
    price_at: Callable[[str, datetime], float | None],
    horizons_minutes: Sequence[float],
    now: datetime,
) -> dict[str, float]:
    """% of buys whose token price was higher ``h`` minutes after entry."""
    out: dict[str, float] = {}
    for h in horizons_minutes:
        wins = total = 0
        for mint, ts, entry_price in buys:
            target = ts + timedelta(minutes=h)
            if target > now or entry_price <= 0:
                continue
            later = price_at(mint, target)
            if later is None:
                continue
            total += 1
            wins += later > entry_price
        if total >= 5:
            out[_fmt_minutes(h)] = wins / total
    return out
