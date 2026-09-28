"""What would a COPIER have earned on each of a wallet's round trips?

A wallet's own PnL is measured at its own fill prices. A copier of that wallet:

1. buys ``latency`` seconds after the source, when the source's own buy has
   already pushed the pool price up (the price impact of their size);
2. meanwhile the price keeps moving: we assume it travels from the source's
   entry to its exit along a straight line in log-space over the holding time,
   so arriving ``latency`` seconds late forfeits ``latency / holding`` of the
   move — all of it when the wallet holds for less than our latency;
3. pays its own price impact and the configured extra slippage on both sides;
4. sells after each source sell, i.e. after that sell's own price impact;
5. pays the fixed network cost of the round trip (``execution.costs``).

Price impact uses a constant-product approximation: a trade of value ``v`` in
a pool with liquidity ``L`` (both sides) moves the price by about ``v / (L/2)``.
Liquidity comes from the trade when known, else the token's current liquidity,
and is never assumed below ``risk.min_liquidity_usd`` (tokens below that are
not copied anyway).

This is an *estimate* — there is no tick-level price history. Its purpose is to
rank wallets by what is replicable with YOUR latency, size and costs, and to
stop copying wallets whose edge disappears once those are paid.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from copytrader.analysis import stats
from copytrader.config.models import AppConfig
from copytrader.core.models import ClosedTrade
from copytrader.execution.costs import lamports_to_usd, round_trip_cost_lamports, swap_fee_lamports

MAX_IMPACT = 0.9
# Only used when no SOL price is available (network costs are denominated in SOL).
FALLBACK_SOL_PRICE_USD = 150.0


@dataclass(frozen=True, slots=True)
class ReplicationParams:
    latency_seconds: float
    size_usd: float
    fixed_cost_frac: float  # round-trip network cost / size
    slippage_frac: float  # extra slippage per side
    min_liquidity_usd: float
    latency_source: str = "config"  # "measured" when taken from our own recent copies

    def describe(self) -> dict[str, Any]:
        return {
            "latency_seconds": round(self.latency_seconds, 2),
            "latency_source": self.latency_source,
            "size_usd": round(self.size_usd, 2),
            "fixed_cost_pct": round(100 * self.fixed_cost_frac, 3),
            "slippage_pct": round(100 * self.slippage_frac, 3),
        }


def typical_size_usd(cfg: AppConfig) -> float:
    """Position size the sizing would use before confidence/volatility adjustments."""
    r = cfg.risk
    if cfg.sizing.method == "fixed":
        size = cfg.sizing.fixed_size_usd
    else:
        size = r.capital_usd * r.max_risk_per_trade_pct / max(cfg.exits.stop_loss_pct, 1e-9)
    return max(r.min_trade_usd, min(r.max_trade_usd, size))


def build_params(
    cfg: AppConfig,
    *,
    latency_seconds: float,
    sol_price_usd: float | None,
    latency_source: str = "config",
    swap_fee: Callable[[float, float], int] | None = None,
) -> ReplicationParams:
    """``swap_fee(size_usd, sol_price)``: expected network cost of one swap (default: static model)."""
    size = cfg.analysis.replication_size_usd or typical_size_usd(cfg)
    sol_price = sol_price_usd or FALLBACK_SOL_PRICE_USD
    fee = swap_fee(size, sol_price) if swap_fee else swap_fee_lamports(cfg, size, sol_price)
    cost_usd = lamports_to_usd(round_trip_cost_lamports(cfg, fee), sol_price)
    return ReplicationParams(
        latency_seconds=max(0.0, latency_seconds),
        size_usd=size,
        fixed_cost_frac=cost_usd / size,
        slippage_frac=cfg.paper.extra_slippage_bps / 10_000,
        min_liquidity_usd=cfg.risk.min_liquidity_usd,
        latency_source=latency_source,
    )


def replicated_return(trade: ClosedTrade, p: ReplicationParams, token_liquidity: float | None = None) -> float | None:
    """Estimated return of copying ``trade`` (fraction; -0.1 = -10 %), or None if not computable."""
    exit_price = trade.exit_price_usd
    if trade.entry_price_usd <= 0 or not exit_price or exit_price <= 0:
        return None
    half_pool = max(trade.liquidity_at_entry_usd or token_liquidity or 0.0, p.min_liquidity_usd) / 2
    source_buy = min((trade.entry_value_usd or trade.cost_usd) / half_pool, MAX_IMPACT)
    source_sell = min(trade.proceeds_usd / max(trade.n_sells, 1) / half_pool, MAX_IMPACT)
    ours = min(p.size_usd / half_pool, MAX_IMPACT)
    late = min(p.latency_seconds / max(trade.holding_seconds, 1.0), 1.0)
    move = exit_price / trade.entry_price_usd
    entry = trade.entry_price_usd * (1 + source_buy) * move**late * (1 + ours) * (1 + p.slippage_frac)
    exit_ = exit_price * (1 - source_sell) * (1 - ours) * (1 - p.slippage_frac)
    return max(-1.0, exit_ / entry - 1 - p.fixed_cost_frac)


@dataclass(slots=True)
class CopyStats:
    n: int = 0
    expectancy_pct: float | None = None
    expectancy_lb_pct: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    copy_cost_pct: float | None = None  # mean (original − copied) return: what copying costs


def copy_stats(
    trades: Sequence[ClosedTrade],
    copied: Sequence[float | None],
    *,
    weights: Sequence[float] | None = None,
    z: float = 1.645,
) -> CopyStats:
    rows = [
        (c, t.return_frac, weights[i] if weights is not None else 1.0)
        for i, (t, c) in enumerate(zip(trades, copied, strict=True))
        if c is not None
    ]
    out = CopyStats(n=len(rows))
    if not rows:
        return out
    values = [r[0] for r in rows]
    originals = [r[1] for r in rows]
    w = [r[2] for r in rows] if weights is not None else None
    out.expectancy_pct = 100 * (stats.mean(values, w) or 0.0)
    lb = stats.mean_lower_bound(values, z, w)
    out.expectancy_lb_pct = None if lb is None else 100 * lb
    wsum = sum(w) if w else float(len(values))
    wins = sum((w[i] if w else 1.0) for i, v in enumerate(values) if v > 0)
    out.win_rate = wins / wsum if wsum > 0 else None
    gains = sum((w[i] if w else 1.0) * v for i, v in enumerate(values) if v > 0)
    losses = -sum((w[i] if w else 1.0) * v for i, v in enumerate(values) if v < 0)
    out.profit_factor = stats.safe_ratio(gains, losses)
    out.copy_cost_pct = 100 * ((stats.mean(originals, w) or 0.0) - (stats.mean(values, w) or 0.0))
    return out
