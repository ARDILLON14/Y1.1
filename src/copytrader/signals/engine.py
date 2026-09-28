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
from copytrader.db.models import Signal, WalletMetric
from copytrader.db.repositories import (
    AnalyticsRepo,
    EventLogRepo,
    PositionRepo,
    SignalRepo,
    TransactionRepo,
    WalletRepo,
)
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
    # False for a wallet that is no longer tracked but still has open copied
    # positions: it keeps being watched, only to mirror its exits.
    tracked: bool = True
    median_hold_seconds: float | None = None  # sets how fresh a signal from it must be
    copy_edge_pct: float | None = None  # expected return per copy (effective: estimate + real copies)
    model_cost_pct: float | None = None  # round-trip network cost the estimate already assumed
    median_win_pct: float | None = None  # its typical winning trade (exit profile)


def signal_age_limit(cfg: AppConfig, median_hold_seconds: float | None) -> tuple[float, str]:
    """Max acceptable delay for a signal from this wallet, and why.

    The global ``max_signal_age_seconds`` is a ceiling. A wallet that holds for
    one minute is not copyable 15 s late, so the limit shrinks to a fraction of
    its median holding time (never below ``min_signal_age_seconds``).
    """
    lat = cfg.latency
    limit = lat.max_signal_age_seconds
    if lat.per_wallet_max_age and median_hold_seconds:
        per_wallet = max(lat.min_signal_age_seconds, lat.max_age_fraction_of_hold * median_hold_seconds)
        if per_wallet < limit:
            return per_wallet, (
                f"{lat.max_age_fraction_of_hold:.0%} de su holding mediano de {_duration(median_hold_seconds)}"
            )
    return limit, "límite global"


def _duration(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _median_hold_seconds(metric: WalletMetric | None) -> float | None:
    minutes = (metric.data or {}).get("median_holding_minutes") if metric is not None else None
    return float(minutes) * 60 if minutes else None


def _copy_edge(metric: WalletMetric | None) -> float | None:
    data = (metric.data or {}) if metric is not None else {}
    value = data.get("effective_copy_expectancy_pct")
    if value is None:
        value = data.get("copy_expectancy_pct")
    return float(value) if value is not None else None


def _median_win(metric: WalletMetric | None) -> float | None:
    value = (metric.data or {}).get("median_win_pct") if metric is not None else None
    return float(value) if value else None


def credible(info: WalletInfo | None) -> bool:
    """A wallet whose trades count as independent evidence (confluence, exits by several wallets).

    Blocked wallets (wash trading, coordinated clusters…) are not independent, and wallets without
    a copyable edge are noise.
    """
    if info is None or info.status is WalletStatus.BLOCKED:
        return False
    return info.status is WalletStatus.ACTIVE or (info.copy_edge_pct or 0.0) > 0


def _model_cost(metric: WalletMetric | None) -> float | None:
    value = ((metric.data or {}).get("replication") or {}).get("fixed_cost_pct") if metric is not None else None
    return float(value) if value is not None else None


def copyable(info: WalletInfo) -> bool:
    """Single definition of "we may copy this wallet's buys" (engine and pipeline agree)."""
    return (
        info.tracked
        and info.selected
        and info.status is WalletStatus.ACTIVE
        and info.list_type not in (ListType.BLACKLIST, ListType.WATCHLIST)
    )


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
        """Tracked wallets, plus untracked ones that still have open copied positions.

        Untracking a wallet must never orphan a position: its sells keep being
        followed (EXIT only) until every position copied from it is closed.
        """
        async with self.db.session() as s:
            rows = list(await WalletRepo(s).list())
            metrics = await AnalyticsRepo(s).latest_metrics_all("all")
            tracked_ids = {w.id for w in rows}
            open_ids = {p.source_wallet_id for p in await PositionRepo(s).open_positions() if p.source_wallet_id}
            for wallet_id in open_ids - tracked_ids:
                w = await WalletRepo(s).get(wallet_id)
                if w is not None:
                    rows.append(w)
        self._wallets = {
            w.address: WalletInfo(
                id=w.id,
                address=w.address,
                label=w.label,
                list_type=ListType(w.list_type),
                status=WalletStatus(w.status),
                score=w.score,
                selected=w.selected and w.is_tracked,
                exit_mode_override=ExitMode(w.exit_mode_override) if w.exit_mode_override else None,
                tracked=w.id in tracked_ids,
                median_hold_seconds=_median_hold_seconds(metrics.get(w.id)),
                copy_edge_pct=_copy_edge(metrics.get(w.id)),
                model_cost_pct=_model_cost(metrics.get(w.id)),
                median_win_pct=_median_win(metrics.get(w.id)),
            )
            for w in rows
        }

    @property
    def tracked_addresses(self) -> set[str]:
        return set(self._wallets)

    def wallet(self, address: str) -> WalletInfo | None:
        return self._wallets.get(address)

    def wallet_by_id(self, wallet_id: int) -> WalletInfo | None:
        return next((w for w in self._wallets.values() if w.id == wallet_id), None)

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
        if not info.tracked:
            return SignalAction.IGNORE, "Wallet no seguida (solo se vigilan sus salidas)"
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
        if copyable(info) and swap.side is Side.BUY:
            if level >= OperatingLevel.PAPER:
                return SignalAction.COPY, "Wallet seleccionada compra"
            return SignalAction.ALERT, "Nivel 2: solo alertas"
        if info.status is WalletStatus.OBSERVE and cfg.selection.alert_on_observe:
            return SignalAction.ALERT, "Wallet en observación"
        if swap.side is Side.SELL and copyable(info):
            return SignalAction.IGNORE, "Venta sin posición copiada abierta"
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
                if signal_id is not None:
                    await EventLogRepo(s).add(
                        "signals",
                        "signal_detected",
                        trace_id=trace_id,
                        ts=detected_at,
                        data={
                            "signal_id": signal_id,
                            "wallet": swap.wallet,
                            "token": swap.token_mint,
                            "side": swap.side.value,
                            "action": action.value,
                            "reason": reason,
                            "source_value_usd": swap.value_usd,
                            "detection_latency_ms": swap.detection_latency_ms,
                            "source": swap.source.value,
                        },
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
