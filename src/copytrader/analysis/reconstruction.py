"""Rebuild a wallet's positions and round trips from its swap history.

Method: average cost per token. A *round trip* (closed trade) starts when the
position goes from flat to non-flat and ends when it returns to (near) flat.
All partial buys and sells in between belong to the same trade.

Sells without a known prior buy (tokens acquired before the history window,
via transfer or airdrop) have an unknown cost basis and are excluded from
performance metrics — counting them would inflate PnL.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from copytrader.core.models import ClosedTrade, OpenLot, SwapEvent
from copytrader.core.types import Side


@dataclass(slots=True)
class _Cycle:
    opened_at: datetime
    entry_price_usd: float
    liquidity_at_entry: float | None
    entry_value: float = 0.0
    qty: float = 0.0
    sold_qty: float = 0.0
    cost: float = 0.0  # remaining cost basis
    cycle_cost: float = 0.0  # total cost of every buy in the cycle
    proceeds: float = 0.0
    max_qty: float = 0.0
    n_buys: int = 0
    n_sells: int = 0
    last_trade_at: datetime | None = None


@dataclass(slots=True)
class Reconstruction:
    closed: list[ClosedTrade] = field(default_factory=list)
    open_lots: list[OpenLot] = field(default_factory=list)
    unmatched_sells: int = 0
    unpriced_swaps: int = 0
    peak_deployed_usd: float = 0.0
    n_swaps: int = 0
    n_buys: int = 0
    n_sells: int = 0


def reconstruct(
    wallet: str,
    swaps: Iterable[SwapEvent],
    *,
    dust_fraction: float = 0.01,
    min_trade_usd: float = 0.0,
    stale_before: datetime | None = None,
    category_of: Callable[[str], str | None] | None = None,
    regime_of: Callable[[datetime], str | None] | None = None,
) -> Reconstruction:
    result = Reconstruction()
    cycles: dict[str, _Cycle] = {}
    deployed = 0.0
    ordered = sorted(swaps, key=lambda s: (s.block_time, s.slot, 0 if s.side is Side.BUY else 1))
    for sw in ordered:
        result.n_swaps += 1
        if sw.side is Side.BUY:
            result.n_buys += 1
        else:
            result.n_sells += 1
        if sw.value_usd is None or sw.value_usd <= 0 or sw.token_amount <= 0:
            result.unpriced_swaps += 1
            continue
        if sw.value_usd < min_trade_usd:
            continue
        cyc = cycles.get(sw.token_mint)
        if sw.side is Side.BUY:
            if cyc is None:
                cyc = _Cycle(
                    opened_at=sw.block_time,
                    entry_price_usd=sw.value_usd / sw.token_amount,
                    liquidity_at_entry=sw.liquidity_usd,
                    entry_value=sw.value_usd,
                )
                cycles[sw.token_mint] = cyc
            cyc.qty += sw.token_amount
            cyc.cost += sw.value_usd
            cyc.cycle_cost += sw.value_usd
            cyc.max_qty = max(cyc.max_qty, cyc.qty)
            cyc.n_buys += 1
            cyc.last_trade_at = sw.block_time
            deployed += sw.value_usd
            result.peak_deployed_usd = max(result.peak_deployed_usd, deployed)
            continue

        # SELL
        if cyc is None or cyc.qty <= 0:
            result.unmatched_sells += 1
            continue
        sell_qty = min(sw.token_amount, cyc.qty)
        matched_proceeds = sw.value_usd * (sell_qty / sw.token_amount)
        cost_portion = cyc.cost * (sell_qty / cyc.qty)
        cyc.proceeds += matched_proceeds
        cyc.cost -= cost_portion
        cyc.qty -= sell_qty
        cyc.sold_qty += sell_qty
        cyc.n_sells += 1
        cyc.last_trade_at = sw.block_time
        deployed = max(0.0, deployed - cost_portion)
        if sw.token_amount > sell_qty * 1.01:
            result.unmatched_sells += 1  # part of this sell had no known cost basis
        if cyc.qty <= dust_fraction * cyc.max_qty:
            deployed = max(0.0, deployed - cyc.cost)
            pnl = cyc.proceeds - cyc.cycle_cost
            result.closed.append(
                ClosedTrade(
                    wallet=wallet,
                    token_mint=sw.token_mint,
                    opened_at=cyc.opened_at,
                    closed_at=sw.block_time,
                    cost_usd=cyc.cycle_cost,
                    proceeds_usd=cyc.proceeds,
                    pnl_usd=pnl,
                    return_frac=pnl / cyc.cycle_cost if cyc.cycle_cost > 0 else 0.0,
                    holding_seconds=(sw.block_time - cyc.opened_at).total_seconds(),
                    n_buys=cyc.n_buys,
                    n_sells=cyc.n_sells,
                    entry_price_usd=cyc.entry_price_usd,
                    category=category_of(sw.token_mint) if category_of else None,
                    liquidity_at_entry_usd=cyc.liquidity_at_entry,
                    regime=regime_of(cyc.opened_at) if regime_of else None,
                    entry_value_usd=cyc.entry_value,
                    exit_price_usd=cyc.proceeds / cyc.sold_qty if cyc.sold_qty > 0 else None,
                )
            )
            del cycles[sw.token_mint]

    for mint, cyc in cycles.items():
        if cyc.qty > 0:
            result.open_lots.append(
                OpenLot(
                    wallet=wallet,
                    token_mint=mint,
                    qty=cyc.qty,
                    cost_usd=cyc.cost,
                    opened_at=cyc.opened_at,
                    last_trade_at=cyc.last_trade_at or cyc.opened_at,
                    stale=bool(stale_before and (cyc.last_trade_at or cyc.opened_at) < stale_before),
                )
            )
    result.closed.sort(key=lambda t: t.closed_at)
    return result
