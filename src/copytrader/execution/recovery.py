"""Crash/restart recovery and on-chain reconciliation.

On startup (before any new signal is processed):
* paper orders interrupted mid-way → FAILED (nothing on-chain);
* live orders never signed (CREATED/QUOTED) → CANCELLED (nothing was sent);
* live orders SIGNED/SUBMITTED → resolved by signature: confirmed → the fill
  is applied exactly once; failed/expired → marked final. Never re-sent as a
  new transaction.
* positions stuck in ``closing`` without an in-flight exit → back to ``open``.

Periodically: resolve pending live orders and compare live positions with the
wallet's real on-chain token balances.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

import structlog

from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.errors import CopyTraderError
from copytrader.core.events import EventBus, SystemMessage
from copytrader.core.models import ExecutionResult, OrderRequest
from copytrader.core.types import (
    KillSwitchScope,
    OrderPurpose,
    OrderStatus,
    PositionStatus,
    Severity,
    Side,
    TradeMode,
)
from copytrader.db.base import Database
from copytrader.db.models import Order
from copytrader.db.repositories import OrderRepo, PositionRepo, RiskEventRepo
from copytrader.execution.live import LiveExecutor
from copytrader.execution.service import ExecutionService
from copytrader.providers.interfaces import TokenInfoProvider
from copytrader.risk.killswitch import KillSwitchService

log = structlog.get_logger(__name__)


def request_from_order(order: Order) -> OrderRequest:
    ctx = order.context or {}
    return OrderRequest(
        client_order_id=order.client_order_id, purpose=OrderPurpose(order.purpose), side=Side(order.side),
        mode=TradeMode(order.mode), token_mint=order.token_mint, token_decimals=int(ctx.get("decimals") or 6),
        input_mint=order.input_mint, output_mint=order.output_mint, amount_in_raw=order.amount_in_raw,
        slippage_bps=order.slippage_bps, signal_id=order.signal_id, position_id=order.position_id,
        theoretical_price_usd=ctx.get("theoretical_price_usd"), notional_usd=order.notional_usd,
        trace_id=order.trace_id)


class OrderRecovery:
    def __init__(self, *, db: Database, clock: Clock, config: Callable[[], AppConfig], bus: EventBus,
                 execution: ExecutionService, live: LiveExecutor | None, tokens: TokenInfoProvider,
                 kill: KillSwitchService) -> None:
        self.db = db
        self.clock = clock
        self._config = config
        self.bus = bus
        self.execution = execution
        self.live = live
        self.tokens = tokens
        self.kill = kill
        self._stopped = asyncio.Event()

    async def on_startup(self) -> dict[str, int]:
        counts = {"paper_failed": 0, "cancelled": 0, "resolved": 0, "pending": 0, "positions_reopened": 0}
        async with self.db.session() as s:
            orders = list(await OrderRepo(s).in_flight())
        for order in orders:
            if order.mode == TradeMode.PAPER.value:
                await self.execution.finalize(order.id, ExecutionResult(
                    success=False, client_order_id=order.client_order_id, mode=TradeMode.PAPER,
                    error="interrumpida por reinicio"))
                counts["paper_failed"] += 1
            elif order.status in (OrderStatus.CREATED.value, OrderStatus.QUOTED.value) or not order.tx_signature:
                async with self.db.session() as s:
                    await OrderRepo(s).set_status(order.id, OrderStatus.CANCELLED,
                                                  error="reinicio antes de firmar: nunca se envió")
                if self.execution.fill_applier is not None:
                    await self.execution.fill_applier.on_order_failed(order, "cancelada por reinicio")
                counts["cancelled"] += 1
            else:
                state = await self.resolve(order)
                counts["resolved" if state != "pending" else "pending"] += 1
        counts["positions_reopened"] = await self._reopen_orphan_closing()
        if any(counts.values()):
            log.warning("recovery_summary", **counts)
        return counts

    async def _reopen_orphan_closing(self) -> int:
        reopened = 0
        async with self.db.session() as s:
            in_flight_positions = {o.position_id for o in await OrderRepo(s).in_flight() if o.position_id}
            for pos in await PositionRepo(s).open_positions():
                if pos.status == PositionStatus.CLOSING.value and pos.id not in in_flight_positions:
                    pos.status = PositionStatus.OPEN.value
                    reopened += 1
        return reopened

    async def resolve(self, order: Order) -> str:
        if self.live is None or not order.tx_signature:
            log.error("cannot_resolve_live_order", order=order.client_order_id)
            return "pending"
        state = await self.live.signature_state(order.tx_signature, order.last_valid_block_height)
        if state == "confirmed":
            sol_price = await self.tokens.sol_price() or 0.0
            try:
                result = await self.live.fill_from_chain(request_from_order(order), order.tx_signature, sol_price)
            except CopyTraderError as exc:
                log.warning("recovery_fill_unreadable", order=order.client_order_id, error=str(exc))
                return "pending"
            await self.execution.finalize(order.id, result)
        elif state != "pending":
            await self.execution.finalize(order.id, ExecutionResult(
                success=False, client_order_id=order.client_order_id, mode=TradeMode.LIVE,
                tx_signature=order.tx_signature, error="expired" if state == "expired" else state))
        return state

    async def resolve_pending(self) -> None:
        async with self.db.session() as s:
            orders = [o for o in await OrderRepo(s).in_flight(TradeMode.LIVE)
                      if o.status in (OrderStatus.SIGNED.value, OrderStatus.SUBMITTED.value)]
        for order in orders:
            try:
                await self.resolve(order)
            except CopyTraderError as exc:
                log.warning("resolve_pending_failed", order=order.client_order_id, error=str(exc))

    async def reconcile_balances(self) -> list[dict[str, Any]]:
        """Compare open live positions with real token balances of the bot wallet."""
        if self.live is None:
            return []
        async with self.db.session() as s:
            positions = list(await PositionRepo(s).open_positions(TradeMode.LIVE))
        if not positions:
            return []
        expected: dict[str, int] = {}
        for p in positions:
            expected[p.token_mint] = expected.get(p.token_mint, 0) + p.qty_raw
        try:
            onchain = await self.live.chain.get_token_balances(self.live.wallet)
        except CopyTraderError as exc:
            log.warning("reconcile_balances_failed", error=str(exc))
            return []
        mismatches = []
        for mint, qty in expected.items():
            actual = onchain.get(mint, 0)
            if actual < qty * 0.99:
                mismatches.append({"mint": mint, "expected_raw": qty, "onchain_raw": actual})
        if mismatches:
            detail = "; ".join(f"{m['mint'][:8]}… esperado {m['expected_raw']} vs on-chain {m['onchain_raw']}"
                               for m in mismatches)
            async with self.db.session() as s:
                await RiskEventRepo(s).add("reconciliation_mismatch", "critical", detail, {"items": mismatches})
            self.bus.publish(SystemMessage(title="Discrepancia de posiciones on-chain", body=detail,
                                           severity=Severity.CRITICAL))
            if self._config().risk.kill_on_reconciliation_mismatch:
                await self.kill.activate(KillSwitchScope.GLOBAL, f"Discrepancia on-chain: {detail[:150]}")
        return mismatches

    async def run(self) -> None:
        loops = 0
        while not self._stopped.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(),
                                       timeout=self._config().execution.reconcile_interval_seconds)
            if self._stopped.is_set():
                break
            try:
                await self.resolve_pending()
                loops += 1
                if loops % 10 == 0:
                    await self.reconcile_balances()
            except Exception:
                log.exception("recovery_loop_failed")

    async def stop(self) -> None:
        self._stopped.set()
