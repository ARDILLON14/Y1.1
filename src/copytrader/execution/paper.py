"""Paper trading executor.

Uses a *real* quote for our exact size (Jupiter in live-data mode, the AMM
model in simulation) so the simulated fill includes the true price impact, then
adds a configurable latency slippage and network fees. The result records the
signal price, theoretical (mid) price, quoted price and simulated fill price —
exactly the comparison the operator needs before risking real money.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.errors import CopyTraderError, ExecutionError
from copytrader.core.models import ExecutionResult, OrderRequest, Quote
from copytrader.core.types import OrderStatus, Side, TradeMode
from copytrader.execution.base import OrderHandle, quote_price_usd, slippage_bps
from copytrader.providers.interfaces import QuoteSource, TokenInfoProvider


class PaperExecutor:
    mode = TradeMode.PAPER

    def __init__(self, quotes: QuoteSource, tokens: TokenInfoProvider, clock: Clock,
                 config: Callable[[], AppConfig]) -> None:
        self.quotes = quotes
        self.tokens = tokens
        self.clock = clock
        self._config = config

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int) -> Quote:
        return await self.quotes.quote(input_mint, output_mint, amount_raw, slippage_bps)

    async def run(self, handle: OrderHandle, req: OrderRequest, quote: Quote | None) -> ExecutionResult:
        cfg = self._config()
        started = time.perf_counter()
        max_age = cfg.latency.max_quote_age_seconds
        if quote is None or (self.clock.now() - quote.obtained_at).total_seconds() > max_age:
            try:
                quote = await self.quote(req.input_mint, req.output_mint, req.amount_in_raw, req.slippage_bps)
            except CopyTraderError as exc:
                raise ExecutionError(f"sin cotización: {exc}", retryable=True) from exc
        await handle.mark(OrderStatus.QUOTED, expected_out_raw=quote.out_amount_raw,
                          min_out_raw=quote.min_out_amount_raw)
        if cfg.paper.simulated_latency_ms > 0:
            await asyncio.sleep(cfg.paper.simulated_latency_ms / 1000)
        sol_price = await self.tokens.sol_price()
        if not sol_price:
            raise ExecutionError("precio de SOL no disponible", retryable=True)
        out_raw = int(quote.out_amount_raw * (1 - cfg.paper.extra_slippage_bps / 10_000))
        if out_raw < quote.min_out_amount_raw:
            return ExecutionResult(success=False, client_order_id=req.client_order_id, mode=self.mode,
                                   error="slippage simulado supera la tolerancia (la tx fallaría on-chain)")
        fees_usd = cfg.paper.network_fee_sol * sol_price
        if cfg.execution.jito_tip_lamports:
            fees_usd += cfg.execution.jito_tip_lamports / 1e9 * sol_price
        if req.side is Side.BUY:
            qty = out_raw / 10 ** req.token_decimals
            value = req.amount_in_raw / 1e9 * sol_price
        else:
            qty = req.amount_in_raw / 10 ** req.token_decimals
            value = out_raw / 1e9 * sol_price
        fill_price = value / qty if qty > 0 else None
        q_price = quote_price_usd(req, quote, sol_price)
        return ExecutionResult(
            success=True, client_order_id=req.client_order_id, mode=self.mode,
            tx_signature=f"paper-{req.client_order_id}", in_amount_raw=req.amount_in_raw, out_amount_raw=out_raw,
            token_qty=qty, fill_price_usd=fill_price, value_usd=value, fees_usd=fees_usd, quote_price_usd=q_price,
            slippage_bps=slippage_bps(req.side, fill_price, req.theoretical_price_usd),
            price_impact_bps=quote.price_impact_frac * 10_000,
            latency_ms=(time.perf_counter() - started) * 1000, executed_at=self.clock.now(),
        )
