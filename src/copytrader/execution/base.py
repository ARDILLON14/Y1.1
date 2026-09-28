"""Executor contract and the order handle used to persist state transitions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from copytrader.core.models import ExecutionResult, OrderRequest, Quote
from copytrader.core.types import OrderStatus, Side, TradeMode
from copytrader.resilience.rate_limiter import Priority


@dataclass(slots=True)
class OrderHandle:
    """Given to executors so they can persist progress *before* irreversible steps."""

    order_id: int
    client_order_id: str
    _mark: Callable[[int, OrderStatus, dict[str, Any]], Awaitable[None]]

    async def mark(self, status: OrderStatus, **fields: Any) -> None:
        await self._mark(self.order_id, status, fields)


class Executor(Protocol):
    mode: TradeMode

    async def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
        *,
        priority: int = Priority.EXECUTION,
    ) -> Quote: ...

    async def run(self, handle: OrderHandle, req: OrderRequest, quote: Quote | None) -> ExecutionResult: ...


def quote_price_usd(req: OrderRequest, q: Quote, sol_price_usd: float, sol_decimals: int = 9) -> float | None:
    """USD price per token implied by a quote (quote asset is SOL)."""
    if req.side is Side.BUY:
        tokens = q.out_amount_raw / 10**req.token_decimals
        usd = q.in_amount_raw / 10**sol_decimals * sol_price_usd
    else:
        tokens = q.in_amount_raw / 10**req.token_decimals
        usd = q.out_amount_raw / 10**sol_decimals * sol_price_usd
    return usd / tokens if tokens > 0 else None


def slippage_bps(side: Side, fill_price: float | None, reference: float | None) -> float | None:
    """Positive = worse than reference (paid more on buys / received less on sells)."""
    if not fill_price or not reference:
        return None
    if side is Side.BUY:
        return (fill_price / reference - 1) * 10_000
    return (1 - fill_price / reference) * 10_000
