"""Live executor for Solana via Jupiter.

Safety properties:
* The order row exists (unique ``client_order_id``) before anything happens.
* After signing, the **signature and lastValidBlockHeight are persisted before
  the first send**. On Solana the signature *is* the transaction id and a
  blockhash-bound tx can never land after ``lastValidBlockHeight``; so after a
  crash we can always tell whether the order executed, and we never create a
  second transaction for the same order.
* Re-sending the same signed bytes is idempotent (same signature).
* ``minOutAmount`` inside the swap bounds the worst possible fill on-chain.
* A confirmation timeout does NOT mark the order failed: it stays SUBMITTED
  and the recovery loop resolves it (confirmed or expired) later.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import structlog

from copytrader.config import hard_limits as HL
from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.errors import CopyTraderError, ExecutionError, SecurityError
from copytrader.core.models import ExecutionResult, OrderRequest, Quote
from copytrader.core.types import OrderPurpose, OrderStatus, Side, TradeMode
from copytrader.execution.base import OrderHandle, quote_price_usd, slippage_bps
from copytrader.execution.costs import lamports_to_usd
from copytrader.providers.interfaces import ChainClient, QuoteSource, SwapTxBuilder, TokenInfoProvider
from copytrader.providers.solana.constants import TOKEN_ACCOUNT_RENT_LAMPORTS
from copytrader.providers.solana.parser import created_token_accounts, parse_swaps
from copytrader.resilience.rate_limiter import Priority
from copytrader.security.signer import Signer
from copytrader.security.signer_policy import SignIntent

log = structlog.get_logger(__name__)


class LiveExecutor:
    mode = TradeMode.LIVE

    def __init__(
        self,
        *,
        quotes: QuoteSource,
        builder: SwapTxBuilder,
        chain: ChainClient,
        signer: Signer,
        tokens: TokenInfoProvider,
        clock: Clock,
        config: Callable[[], AppConfig],
    ) -> None:
        self.quotes = quotes
        self.builder = builder
        self.chain = chain
        self.signer = signer
        self.tokens = tokens
        self.clock = clock
        self._config = config

    @property
    def wallet(self) -> str:
        wallet = self._config().execution.wallet_public_key
        if not wallet:
            raise ExecutionError("execution.wallet_public_key no configurada")
        return wallet

    async def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
        *,
        priority: int = Priority.EXECUTION,
    ) -> Quote:
        return await self.quotes.quote(input_mint, output_mint, amount_raw, slippage_bps, priority=priority)

    def _validate_quote(self, req: OrderRequest, q: Quote, sol_price: float) -> None:
        cfg = self._config()
        if q.in_amount_raw != req.amount_in_raw or q.input_mint != req.input_mint or q.output_mint != req.output_mint:
            raise ExecutionError("la cotización no corresponde a la orden")
        max_impact = (
            cfg.risk.max_slippage_pct
            if req.purpose is OrderPurpose.ENTRY
            else min(cfg.exits.exit_slippage_pct, HL.HARD_MAX_EXIT_SLIPPAGE_PCT)
        )
        if q.price_impact_frac * 100 > max_impact:
            raise ExecutionError(f"impacto de precio {q.price_impact_frac * 100:.2f}% > {max_impact:.2f}%")
        if req.purpose is OrderPurpose.ENTRY and req.theoretical_price_usd and req.max_price_deviation_pct:
            price = quote_price_usd(req, q, sol_price)
            if price is None:
                raise ExecutionError("precio de cotización no calculable")
            dev = (price / req.theoretical_price_usd - 1) * 100
            if dev > req.max_price_deviation_pct:
                raise ExecutionError(f"el precio se movió {dev:.2f}% > {req.max_price_deviation_pct:.2f}%")

    async def _check_balance(self, req: OrderRequest) -> None:
        if req.side is not Side.BUY:
            return
        reserve = int(max(self._config().risk.reserve_sol, HL.HARD_MIN_RESERVE_SOL) * 1e9)
        balance = await self.chain.get_balance(self.wallet)
        if balance - req.amount_in_raw < reserve:
            raise ExecutionError(f"saldo insuficiente: {balance / 1e9:.4f} SOL (reserva {reserve / 1e9:.3f})")

    async def run(self, handle: OrderHandle, req: OrderRequest, quote: Quote | None) -> ExecutionResult:
        cfg = self._config()
        started = time.perf_counter()
        sol_price = await self.tokens.sol_price()
        if not sol_price:
            raise ExecutionError("precio de SOL no disponible", retryable=True)
        await self._check_balance(req)
        if quote is None or (self.clock.now() - quote.obtained_at).total_seconds() > cfg.latency.max_quote_age_seconds:
            try:
                quote = await self.quote(req.input_mint, req.output_mint, req.amount_in_raw, req.slippage_bps)
            except CopyTraderError as exc:
                raise ExecutionError(f"sin cotización: {exc}", retryable=True) from exc
        self._validate_quote(req, quote, sol_price)
        await handle.mark(
            OrderStatus.QUOTED, expected_out_raw=quote.out_amount_raw, min_out_raw=quote.min_out_amount_raw
        )
        try:
            built = await self.builder.build_swap(
                quote,
                self.wallet,
                priority_max_lamports=cfg.execution.priority_fee_max_lamports,
                priority_level=cfg.execution.priority_level,
                jito_tip_lamports=cfg.execution.jito_tip_lamports,
            )
        except CopyTraderError as exc:
            raise ExecutionError(f"no se pudo construir la transacción: {exc}", retryable=True) from exc
        intent = SignIntent(
            client_order_id=req.client_order_id,
            purpose=req.purpose.value,
            input_mint=req.input_mint,
            output_mint=req.output_mint,
            amount_in_raw=req.amount_in_raw,
            notional_usd=float(req.notional_usd or 0.0),
        )
        try:
            signed = await self.signer.sign(built.tx_bytes, intent)
        except SecurityError as exc:
            raise ExecutionError(f"firma rechazada: {exc}") from exc
        # Persist BEFORE sending: from here on the order is identified by its signature.
        await handle.mark(
            OrderStatus.SIGNED, tx_signature=signed.signature, last_valid_block_height=built.last_valid_block_height
        )
        result = await self.send_and_confirm(
            handle, req, signed.tx_bytes, signed.signature, built.last_valid_block_height, sol_price
        )
        result.quote_price_usd = quote_price_usd(req, quote, sol_price)
        result.price_impact_bps = quote.price_impact_frac * 10_000
        result.latency_ms = (time.perf_counter() - started) * 1000
        return result

    async def send_and_confirm(
        self,
        handle: OrderHandle | None,
        req: OrderRequest,
        tx_bytes: bytes,
        signature: str,
        last_valid_block_height: int,
        sol_price: float,
    ) -> ExecutionResult:
        cfg = self._config().execution
        deadline = time.monotonic() + cfg.confirm_timeout_seconds
        last_send = 0.0
        submitted = False
        while True:
            now = time.monotonic()
            if now - last_send >= cfg.rebroadcast_interval_ms / 1000:
                try:
                    await self.chain.send_raw_transaction(tx_bytes, skip_preflight=cfg.skip_preflight)
                except CopyTraderError as exc:
                    log.warning("send_failed_will_retry", signature=signature, error=str(exc))
                last_send = now
                if not submitted and handle is not None:
                    await handle.mark(OrderStatus.SUBMITTED)
                    submitted = True
            state = await self.signature_state(signature, last_valid_block_height)
            if state == "confirmed":
                return await self.fill_from_chain(req, signature, sol_price)
            if state.startswith("failed"):
                return ExecutionResult(
                    success=False,
                    client_order_id=req.client_order_id,
                    mode=self.mode,
                    tx_signature=signature,
                    error=f"transacción fallida on-chain: {state}",
                )
            if state == "expired":
                return ExecutionResult(
                    success=False,
                    client_order_id=req.client_order_id,
                    mode=self.mode,
                    tx_signature=signature,
                    error="expired",
                )
            if now > deadline:
                return ExecutionResult(
                    success=False,
                    client_order_id=req.client_order_id,
                    mode=self.mode,
                    tx_signature=signature,
                    error="pending",
                    retryable=True,
                )
            await asyncio.sleep(0.4)

    async def signature_state(self, signature: str, last_valid_block_height: int | None) -> str:
        """'confirmed' | 'failed:<err>' | 'expired' | 'pending'."""
        try:
            statuses = await self.chain.get_signature_statuses([signature])
        except CopyTraderError:
            return "pending"
        status: dict[str, Any] | None = statuses[0] if statuses else None
        if status:
            if status.get("err"):
                return f"failed:{status['err']}"
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return "confirmed"
        if last_valid_block_height is not None:
            try:
                height = await self.chain.get_block_height()
            except CopyTraderError:
                return "pending"
            if height > last_valid_block_height:
                # Check once more: it may have landed right before expiring.
                statuses = await self.chain.get_signature_statuses([signature])
                again = statuses[0] if statuses else None
                if again and not again.get("err"):
                    return "confirmed"
                return "expired"
        return "pending"

    async def fill_from_chain(self, req: OrderRequest, signature: str, sol_price: float) -> ExecutionResult:
        tx = None
        for attempt in range(10):
            try:
                tx = await self.chain.get_transaction(signature)
            except CopyTraderError:
                tx = None
            if tx:
                break
            await asyncio.sleep(0.3 * (attempt + 1))
        if not tx:
            raise ExecutionError(f"tx {signature} confirmada pero no legible todavía", retryable=True)
        swaps = [
            s
            for s in parse_swaps(
                tx, self.wallet, sol_price_usd=sol_price, quote_mints=self._config().providers.quote_mints
            )
            if s.token_mint == req.token_mint
        ]
        if not swaps:
            raise ExecutionError(f"no se encontró el swap del token en la tx {signature}")
        s = swaps[0]
        fee_usd = s.fee_sol * sol_price
        cfg = self._config()
        if (
            req.side is Side.BUY
            and not cfg.execution.close_empty_token_accounts
            and req.token_mint in created_token_accounts(tx, self.wallet)
        ):
            # The parser leaves refundable rent out of the trade; if we never close the
            # account it is not refundable, so it is a real cost of this entry.
            fee_usd += lamports_to_usd(TOKEN_ACCOUNT_RENT_LAMPORTS, sol_price)
        qty = s.token_amount
        value = s.value_usd or s.quote_amount * sol_price
        token_raw = round(qty * 10**req.token_decimals)
        quote_raw = round(s.quote_amount * 1e9)
        fill_price = value / qty if qty > 0 else None
        return ExecutionResult(
            success=True,
            client_order_id=req.client_order_id,
            mode=self.mode,
            tx_signature=signature,
            in_amount_raw=quote_raw if req.side is Side.BUY else token_raw,
            out_amount_raw=token_raw if req.side is Side.BUY else quote_raw,
            token_qty=qty,
            fill_price_usd=fill_price,
            value_usd=value,
            fees_usd=fee_usd,
            slippage_bps=slippage_bps(req.side, fill_price, req.theoretical_price_usd),
            executed_at=s.block_time,
        )
