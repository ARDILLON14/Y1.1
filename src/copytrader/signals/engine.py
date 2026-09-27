"""Signal Detection Engine.

Receives parsed swaps from the feed, and for each one:

1. persists the swap (idempotent: duplicates end here);
2. classifies it: COPY (selected wallet buys), EXIT (a wallet we copied sells),
   ALERT (watch-only), IGNORE (analytics only) — respecting the operating
   level and the manual lists (blacklist never copies);
3. creates the ``signals`` row in the same DB transaction as the swap, so a
   crash can never leave a stored swap without its signal;
4. dispatches it to a per-token partition queue: events of the same token are
   processed strictly in order (buy before sell), different tokens in parallel.
"""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import structlog

from copytrader.config.models import AppConfig
from copytrader.core import ids
from copytrader.core.clock import Clock
from copytrader.core.events import EventBus, SignalAlert
from copytrader.core.models import SwapEvent
from copytrader.core.types import (
    ExitMode,
    ListType,
    OperatingLevel,
    Side,
    SignalAction,
    SignalStatus,
    WalletStatus,
)
from copytrader.db.base import Database
from copytrader.db.models import Signal
from copytrader.db.repositories import PositionRepo, SignalRepo, TransactionRepo, WalletRepo
from copytrader.db.repositories.wallets import row_to_swap
from copytrader.execution.mode import ModeController
from copytrader.observability import metrics
from copytrader.observability.logging import bind_trace, clear_trace

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WalletInfo:
    id: int
    address: str
    label: str | None
    list_type: ListType
    status: WalletStatus
    score: float | None
    selected: bool
    exit_mode_override: ExitMode | None = None


@dataclass(slots=True)
class SignalContext:
    signal_id: int
    signal_key: str
    trace_id: str
    swap: SwapEvent
    wallet: WalletInfo
    action: SignalAction
    detected_at: datetime


class EntryHandler(Protocol):
    async def process_entry(self, ctx: SignalContext) -> None: ...


class ExitHandler(Protocol):
    async def on_source_sell(self, ctx: SignalContext) -> None: ...


class SignalEngine:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        config: Callable[[], AppConfig],
        bus: EventBus,
        mode: ModeController,
        chain: str = "solana",
    ) -> None:
        self.db = db
        self.clock = clock
        self._config = config
        self.bus = bus
        self.mode = mode
        self.chain = chain
        self.entry_handler: EntryHandler | None = None
        self.exit_handler: ExitHandler | None = None
        self._wallets: dict[str, WalletInfo] = {}
        n = config().signals.partitions
        size = config().signals.queue_size
        self._queues: list[asyncio.Queue[SignalContext]] = [asyncio.Queue(maxsize=size) for _ in range(n)]
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ state
    async def refresh_wallets(self) -> None:
        async with self.db.session() as s:
            rows = await WalletRepo(s).list()
        self._wallets = {
            w.address: WalletInfo(
                id=w.id,
                address=w.address,
                label=w.label,
                list_type=ListType(w.list_type),
                status=WalletStatus(w.status),
                score=w.score,
                selected=w.selected,
                exit_mode_override=ExitMode(w.exit_mode_override) if w.exit_mode_override else None,
            )
            for w in rows
        }

    @property
    def tracked_addresses(self) -> set[str]:
        return set(self._wallets)

    def wallet(self, address: str) -> WalletInfo | None:
        return self._wallets.get(address)

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._worker(q)) for q in self._queues]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def drain(self) -> None:
        """Wait until every queued signal has been processed (tests/shutdown)."""
        for q in self._queues:
            await q.join()

    # ---------------------------------------------------------- classification
    def classify(self, swap: SwapEvent, info: WalletInfo, has_position: bool) -> tuple[SignalAction, str]:
        cfg = self._config()
        level = self.mode.level
        if swap.side is Side.SELL and has_position and cfg.signals.follow_sells:
            return SignalAction.EXIT, "La wallet origen vende un token que copiamos"
        if (swap.value_usd or 0.0) < cfg.signals.min_source_value_usd:
            return SignalAction.IGNORE, "Operación de origen demasiado pequeña"
        if level < OperatingLevel.ALERTS:
            return SignalAction.IGNORE, "Nivel 1: solo análisis"
        if info.list_type is ListType.BLACKLIST:
            return SignalAction.IGNORE, "Wallet en blacklist"
        if info.list_type is ListType.WATCHLIST:
            return (
                (SignalAction.ALERT, "Wallet en watchlist")
                if cfg.selection.alert_on_watchlist
                else (SignalAction.IGNORE, "Watchlist sin alertas")
            )
        copyable = info.selected and info.status is WalletStatus.ACTIVE
        if copyable and swap.side is Side.BUY:
            if level >= OperatingLevel.PAPER:
                return SignalAction.COPY, "Wallet seleccionada compra"
            return SignalAction.ALERT, "Nivel 2: solo alertas"
        if info.status is WalletStatus.OBSERVE and cfg.selection.alert_on_observe:
            return SignalAction.ALERT, "Wallet en observación"
        return SignalAction.IGNORE, "Wallet no seleccionada"

    # --------------------------------------------------------------- ingestion
    async def on_swap(self, swap: SwapEvent) -> None:
        info = self._wallets.get(swap.wallet)
        if info is None:
            return
        detected_at = swap.detected_at or self.clock.now()
        trace_id = ids.new_trace_id()
        key = ids.signal_key(self.chain, swap.wallet, swap.signature, swap.token_mint, swap.side.value)
        async with self.db.session() as s:
            if await TransactionRepo(s).insert_swap(info.id, swap) is None:
                return  # already processed (duplicate notification / catch-up overlap)
            await WalletRepo(s).touch_activity(info.id, swap.block_time, swap.signature, swap.slot)
            has_position = bool(
                swap.side is Side.SELL and await PositionRepo(s).open_from_wallet_token(info.id, swap.token_mint)
            )
            action, reason = self.classify(swap, info, has_position)
            signal_id: int | None = None
            if action is not SignalAction.IGNORE:
                signal_id = await SignalRepo(s).create(
                    {
                        "signal_key": key,
                        "trace_id": trace_id,
                        "wallet_id": info.id,
                        "source_signature": swap.signature,
                        "token_mint": swap.token_mint,
                        "side": swap.side.value,
                        "action": action.value,
                        "status": SignalStatus.DETECTED.value,
                        "source_price_usd": swap.price_usd,
                        "source_value_usd": swap.value_usd,
                        "source_block_time": swap.block_time,
                        "detected_at": detected_at,
                        "detection_latency_ms": swap.detection_latency_ms,
                        "wallet_score": info.score,
                        "operating_level": int(self.mode.level),
                        "reason": reason,
                        "created_at": self.clock.now(),
                    }
                )
        metrics.SIGNALS.labels(action=action.value, status="detected").inc()
        if signal_id is None:
            return
        log.info(
            "signal_detected",
            trace_id=trace_id,
            wallet=swap.wallet,
            token=swap.token_mint,
            side=swap.side.value,
            action=action.value,
            value_usd=swap.value_usd,
            latency_ms=swap.detection_latency_ms,
        )
        await self._enqueue(SignalContext(signal_id, key, trace_id, swap, info, action, detected_at))

    async def _enqueue(self, ctx: SignalContext) -> None:
        idx = zlib.crc32(ctx.swap.token_mint.encode()) % len(self._queues)
        await self._queues[idx].put(ctx)
        metrics.QUEUE_DEPTH.set(sum(q.qsize() for q in self._queues))

    async def _worker(self, queue: asyncio.Queue[SignalContext]) -> None:
        while True:
            ctx = await queue.get()
            bind_trace(ctx.trace_id, signal_id=ctx.signal_id)
            try:
                await self._dispatch(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                metrics.ERRORS.labels(component="signal_engine").inc()
                log.exception("signal_processing_failed")
                await self._set_status(ctx.signal_id, SignalStatus.FAILED, f"Error interno: {type(exc).__name__}")
            finally:
                clear_trace()
                queue.task_done()

    async def _dispatch(self, ctx: SignalContext) -> None:
        if ctx.action is SignalAction.COPY and self.entry_handler is not None:
            await self.entry_handler.process_entry(ctx)
        elif ctx.action is SignalAction.EXIT and self.exit_handler is not None:
            await self.exit_handler.on_source_sell(ctx)
        elif ctx.action is SignalAction.ALERT:
            self.bus.publish(
                SignalAlert(
                    trace_id=ctx.trace_id,
                    wallet=ctx.wallet.address,
                    wallet_label=ctx.wallet.label,
                    wallet_score=ctx.wallet.score,
                    token_mint=ctx.swap.token_mint,
                    token_symbol=None,
                    side=ctx.swap.side.value,
                    price_usd=ctx.swap.price_usd,
                    value_usd=ctx.swap.value_usd,
                    reason=ctx.wallet.list_type.value if ctx.wallet.list_type is not ListType.NONE else "alert",
                )
            )
            await self._set_status(ctx.signal_id, SignalStatus.ALERTED, None)
        else:
            await self._set_status(ctx.signal_id, SignalStatus.IGNORED, "Sin manejador")

    async def _set_status(self, signal_id: int, status: SignalStatus, reason: str | None) -> None:
        async with self.db.session() as s:
            sig = await SignalRepo(s).get(signal_id)
            if sig is not None:
                sig.status = status.value
                if reason:
                    sig.reason = reason
                sig.decided_at = self.clock.now()
        metrics.SIGNALS.labels(action="-", status=status.value).inc()

    # ---------------------------------------------------------------- recovery
    async def recover(self) -> dict[str, int]:
        """After a restart: re-run pending EXIT signals, expire pending entries."""
        from sqlalchemy import select

        from copytrader.db.models import WalletTransaction

        counts = {"requeued_exits": 0, "expired_entries": 0}
        async with self.db.session() as s:
            pending = (
                (await s.execute(select(Signal).where(Signal.status == SignalStatus.DETECTED.value))).scalars().all()
            )
            requeue: list[SignalContext] = []
            for sig in pending:
                if sig.action == SignalAction.EXIT.value:
                    wallet = await WalletRepo(s).get(sig.wallet_id)
                    row = (
                        await s.execute(
                            select(WalletTransaction).where(
                                WalletTransaction.wallet_id == sig.wallet_id,
                                WalletTransaction.signature == sig.source_signature,
                                WalletTransaction.token_mint == sig.token_mint,
                                WalletTransaction.side == sig.side,
                            )
                        )
                    ).scalar_one_or_none()
                    info = self._wallets.get(wallet.address) if wallet else None
                    if wallet is None or row is None or info is None:
                        sig.status = SignalStatus.FAILED.value
                        sig.reason = "Recuperación: datos de origen no disponibles"
                        continue
                    requeue.append(
                        SignalContext(
                            sig.id,
                            sig.signal_key,
                            sig.trace_id,
                            row_to_swap(row, wallet.address),
                            info,
                            SignalAction.EXIT,
                            sig.detected_at,
                        )
                    )
                else:
                    sig.status = SignalStatus.EXPIRED.value
                    sig.reason = "Reinicio del sistema: señal caducada sin ejecutar"
                    counts["expired_entries"] += 1
        for ctx in requeue:
            await self._enqueue(ctx)
            counts["requeued_exits"] += 1
        return counts
