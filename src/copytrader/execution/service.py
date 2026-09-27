"""Execution service: guard → idempotent order → executor → atomic fill.

* ``ExecutionGuard`` re-checks the absolute limits independently of the risk
  engine (defence in depth): a bug upstream still cannot send an oversized
  entry or an entry while a kill switch is on or live trading got disarmed.
* Orders are keyed by a deterministic ``client_order_id``: calling ``execute``
  twice for the same order never sends twice.
* A fill is applied in ONE database transaction together with the execution
  row, which is UNIQUE per order: a fill can never be applied twice, even if
  the recovery loop and the live path race.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from copytrader.config import hard_limits as HL
from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.errors import ExecutionError
from copytrader.core.events import EventBus, ExecutionFailed, TradeExecuted
from copytrader.core.models import ExecutionResult, OrderRequest, Quote
from copytrader.core.types import OrderPurpose, OrderStatus, SignalStatus, TradeMode
from copytrader.db.base import Database
from copytrader.db.models import Execution, Order
from copytrader.db.repositories import EventLogRepo, ExecutionRepo, OrderRepo, SignalRepo
from copytrader.execution.base import Executor, OrderHandle
from copytrader.execution.mode import ModeController
from copytrader.observability import metrics
from copytrader.risk.engine import RiskEngine
from copytrader.risk.killswitch import KillSwitchService

log = structlog.get_logger(__name__)


class FillApplier(Protocol):
    async def apply_fill(self, session: AsyncSession, order: Order, result: ExecutionResult) -> float | None:
        """Update positions for a fill inside ``session``; return realized PnL (exits) or None."""
        ...

    async def on_order_failed(self, order: Order, error: str) -> None: ...


class ExecutionGuard:
    def __init__(
        self, config: Callable[[], AppConfig], mode: ModeController, kill: KillSwitchService, risk: RiskEngine
    ) -> None:
        self._config = config
        self.mode = mode
        self.kill = kill
        self.risk = risk

    def check(self, req: OrderRequest) -> None:
        if req.purpose is OrderPurpose.EXIT:
            if req.slippage_bps > HL.HARD_MAX_EXIT_SLIPPAGE_PCT * 100:
                raise ExecutionError("slippage de salida por encima del límite absoluto")
            return
        limits = self.risk.limits()
        if reason := self.kill.blocking_reason():
            raise ExecutionError(reason)
        notional = req.notional_usd or 0.0
        if notional <= 0:
            raise ExecutionError("orden de entrada sin notional")
        if notional > limits.hard_max_trade_usd * 1.001 or notional > limits.max_trade_usd * 1.001:
            raise ExecutionError(
                f"notional {notional:.2f} supera el máximo permitido "
                f"({min(limits.hard_max_trade_usd, limits.max_trade_usd):.2f})"
            )
        if req.slippage_bps > HL.HARD_MAX_ENTRY_SLIPPAGE_PCT * 100:
            raise ExecutionError("slippage de entrada por encima del límite absoluto")
        if req.mode is TradeMode.LIVE and not self.mode.live_allowed:
            raise ExecutionError(f"trading real no permitido: {self.mode.live_block_reason()}")


class ExecutionService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        config: Callable[[], AppConfig],
        bus: EventBus,
        executors: dict[TradeMode, Executor],
        guard: ExecutionGuard,
        risk: RiskEngine,
    ) -> None:
        self.db = db
        self.clock = clock
        self._config = config
        self.bus = bus
        self.executors = executors
        self.guard = guard
        self.risk = risk
        self.fill_applier: FillApplier | None = None

    def executor(self, mode: TradeMode) -> Executor:
        ex = self.executors.get(mode)
        if ex is None:
            raise ExecutionError(f"no hay ejecutor para el modo {mode.value}")
        return ex

    async def _mark(self, order_id: int, status: OrderStatus, fields: dict[str, Any]) -> None:
        async with self.db.session() as s:
            await OrderRepo(s).set_status(order_id, status, **fields)
        metrics.ORDERS.labels(mode="-", purpose="-", status=status.value).inc()

    async def execute(
        self,
        req: OrderRequest,
        *,
        quote: Quote | None = None,
        context: dict[str, Any] | None = None,
        reservation_id: str | None = None,
        trigger: str | None = None,
    ) -> ExecutionResult:
        try:
            self.guard.check(req)
        except ExecutionError as exc:
            self.risk.release(reservation_id)
            log.error("execution_guard_blocked", order=req.client_order_id, reason=str(exc))
            return ExecutionResult(
                success=False, client_order_id=req.client_order_id, mode=req.mode, error=f"bloqueada por guardia: {exc}"
            )
        async with self.db.session() as s:
            order, created = await OrderRepo(s).create(
                {
                    "client_order_id": req.client_order_id,
                    "trace_id": req.trace_id,
                    "signal_id": req.signal_id,
                    "position_id": req.position_id,
                    "mode": req.mode.value,
                    "purpose": req.purpose.value,
                    "side": req.side.value,
                    "token_mint": req.token_mint,
                    "input_mint": req.input_mint,
                    "output_mint": req.output_mint,
                    "amount_in_raw": req.amount_in_raw,
                    "notional_usd": req.notional_usd,
                    "slippage_bps": req.slippage_bps,
                    "status": OrderStatus.CREATED.value,
                    "attempts": 1,
                    "trigger": trigger,
                    "context": context or {},
                    "created_at": self.clock.now(),
                    "updated_at": self.clock.now(),
                }
            )
            order_id, existing_status = order.id, OrderStatus(order.status)
        # From now on the in-flight order itself counts as exposure in the risk book.
        self.risk.release(reservation_id)
        if not created:
            log.warning("duplicate_order_ignored", order=req.client_order_id, status=existing_status.value)
            return ExecutionResult(
                success=existing_status is OrderStatus.CONFIRMED,
                client_order_id=req.client_order_id,
                mode=req.mode,
                error=None
                if existing_status is OrderStatus.CONFIRMED
                else f"orden duplicada (estado {existing_status.value})",
            )
        handle = OrderHandle(order_id, req.client_order_id, self._mark)
        executor = self.executor(req.mode)
        try:
            result = await executor.run(handle, req, quote)
        except ExecutionError as exc:
            result = ExecutionResult(
                success=False,
                client_order_id=req.client_order_id,
                mode=req.mode,
                error=str(exc),
                retryable=exc.retryable,
            )
        except Exception as exc:  # unexpected executor failure
            log.exception("executor_crashed", order=req.client_order_id)
            result = ExecutionResult(
                success=False,
                client_order_id=req.client_order_id,
                mode=req.mode,
                error=f"error inesperado: {type(exc).__name__}",
            )
        await self.finalize(order_id, result)
        return result

    @staticmethod
    async def _settle_entry_signal(s: AsyncSession, order: Order, status: SignalStatus, reason: str) -> None:
        """An entry left pending (APPROVED) resolves later: reflect the outcome on its signal."""
        if order.purpose != OrderPurpose.ENTRY.value or order.signal_id is None:
            return
        sig = await SignalRepo(s).get(order.signal_id)
        # DETECTED: the pipeline has not recorded its decision yet (it will keep this outcome).
        if sig is not None and sig.status in (SignalStatus.APPROVED.value, SignalStatus.DETECTED.value):
            sig.status = status.value
            sig.reason = reason

    async def finalize(self, order_id: int, result: ExecutionResult) -> None:
        """Apply a successful fill atomically, or record the failure. Idempotent."""
        if result.success:
            async with self.db.session() as s:
                order = await OrderRepo(s).get(order_id)
                if order is None:
                    return
                ctx = dict(order.context or {})
                exec_id = await ExecutionRepo(s).add(
                    {
                        "order_id": order.id,
                        "mode": order.mode,
                        "side": order.side,
                        "token_mint": order.token_mint,
                        "tx_signature": result.tx_signature,
                        "in_amount_raw": result.in_amount_raw,
                        "out_amount_raw": result.out_amount_raw,
                        "token_qty": result.token_qty,
                        "signal_price_usd": ctx.get("signal_price_usd"),
                        "theoretical_price_usd": ctx.get("theoretical_price_usd"),
                        "quote_price_usd": result.quote_price_usd,
                        "fill_price_usd": result.fill_price_usd,
                        "slippage_bps": result.slippage_bps,
                        "price_impact_bps": result.price_impact_bps,
                        "value_usd": result.value_usd,
                        "fees_usd": result.fees_usd,
                        "latency_ms": result.latency_ms,
                        "executed_at": result.executed_at or self.clock.now(),
                    }
                )
                if exec_id is None:
                    return  # fill already applied (recovery/live race): nothing to do
                realized = None
                if self.fill_applier is not None:
                    realized = await self.fill_applier.apply_fill(s, order, result)
                if realized is not None:
                    row = await s.get(Execution, exec_id)
                    if row is not None:
                        row.realized_pnl_usd = realized
                await OrderRepo(s).set_status(order.id, OrderStatus.CONFIRMED, tx_signature=result.tx_signature)
                await EventLogRepo(s).add(
                    "execution",
                    "fill_applied",
                    trace_id=order.trace_id,
                    data={
                        "order_id": order.id,
                        "position_id": order.position_id,
                        "value_usd": result.value_usd,
                        "fill_price_usd": result.fill_price_usd,
                        "slippage_bps": result.slippage_bps,
                        "fees_usd": result.fees_usd,
                        "realized_pnl_usd": realized,
                        "tx_signature": result.tx_signature,
                    },
                )
                await self._settle_entry_signal(s, order, SignalStatus.EXECUTED, "Copiada (confirmación tardía)")
                mode, purpose, side, token_mint = order.mode, order.purpose, order.side, order.token_mint
                pos_id, trace_id = order.position_id, order.trace_id
            metrics.ORDERS.labels(mode=mode, purpose=purpose, status="confirmed").inc()
            if result.latency_ms is not None:
                metrics.EXECUTION_LATENCY.labels(mode=mode).observe(result.latency_ms / 1000)
            self.bus.publish(
                TradeExecuted(
                    trace_id=trace_id,
                    mode=mode,
                    purpose=purpose,
                    side=side,
                    token_mint=token_mint,
                    token_symbol=ctx.get("token_symbol"),
                    value_usd=result.value_usd,
                    fill_price_usd=result.fill_price_usd,
                    slippage_bps=result.slippage_bps,
                    fees_usd=result.fees_usd,
                    tx_signature=result.tx_signature,
                    position_id=pos_id,
                    source_wallet=ctx.get("source_wallet"),
                )
            )
            return

        pending = result.error == "pending"
        status = (
            OrderStatus.SUBMITTED
            if pending
            else OrderStatus.EXPIRED
            if result.error == "expired"
            else OrderStatus.FAILED
        )
        async with self.db.session() as s:
            await OrderRepo(s).set_status(order_id, status, error=result.error)
            order = await OrderRepo(s).get(order_id)
            if order is not None and not pending:
                await self._settle_entry_signal(
                    s, order, SignalStatus.FAILED, f"Orden no ejecutada: {result.error or 'error'}"
                )
        if order is None or pending:
            if pending:
                log.warning("order_pending_confirmation", order=result.client_order_id)
            return
        metrics.ORDERS.labels(mode=order.mode, purpose=order.purpose, status=status.value).inc()
        if self.fill_applier is not None:
            await self.fill_applier.on_order_failed(order, result.error or "error")
        if order.mode == TradeMode.LIVE.value or not (result.error or "").startswith("slippage simulado"):
            await self.risk.record_execution_error(result.error or "error")
        self.bus.publish(
            ExecutionFailed(
                trace_id=order.trace_id,
                mode=order.mode,
                purpose=order.purpose,
                token_mint=order.token_mint,
                error=result.error or "error",
                client_order_id=order.client_order_id,
            )
        )
