"""Position Manager.

* Applies fills to positions (called by the execution service inside the same
  DB transaction as the execution row → exactly-once accounting).
* Watches prices of open positions and applies the exit rules.
* Mirrors the source wallet's sells according to each position's exit mode.
* Manual close and "close everything" (kill switch flatten).

Concurrency: every mutation of a position happens under the per-token lock
shared with the copy pipeline, and a position being closed is marked
``closing`` so two triggers (e.g. stop loss + source sell) cannot both sell it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from copytrader.config.models import AppConfig
from copytrader.core import ids
from copytrader.core.clock import Clock
from copytrader.core.concurrency import KeyedLocks
from copytrader.core.errors import CopyTraderError
from copytrader.core.events import EventBus, PositionClosed, SystemMessage
from copytrader.core.models import CheckResult, Decision, ExecutionResult, OrderRequest
from copytrader.core.types import (
    ExitMode,
    OrderPurpose,
    PositionStatus,
    Severity,
    Side,
    SignalStatus,
    TradeMode,
)
from copytrader.db.base import Database
from copytrader.db.models import Order, Position
from copytrader.db.repositories import EventLogRepo, PositionRepo, SignalRepo, TransactionRepo
from copytrader.execution.service import ExecutionService
from copytrader.observability import metrics
from copytrader.positions.exits import ExitDecision, PositionView, evaluate_exit, source_sell_fraction
from copytrader.providers.interfaces import TokenInfoProvider
from copytrader.signals.engine import SignalContext

log = structlog.get_logger(__name__)
DUST_FRACTION = 0.001
SOURCE_EXIT_GRACE_SECONDS = 20.0


class PositionManager:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        config: Callable[[], AppConfig],
        bus: EventBus,
        execution: ExecutionService,
        tokens: TokenInfoProvider,
        token_locks: KeyedLocks,
    ) -> None:
        self.db = db
        self.clock = clock
        self._config = config
        self.bus = bus
        self.execution = execution
        self.tokens = tokens
        self.locks = token_locks
        self._backoff: dict[int, tuple[float, int]] = {}  # position -> (retry at monotonic, failures)
        self._stale_alerted: set[int] = set()
        self._stopped = asyncio.Event()

    # ================================================================ fills
    async def apply_fill(self, session: AsyncSession, order: Order, result: ExecutionResult) -> float | None:
        ctx = order.context or {}
        repo = PositionRepo(session)
        now = result.executed_at or self.clock.now()
        if order.purpose == OrderPurpose.ENTRY.value:
            cost = result.value_usd + result.fees_usd
            qty_raw = result.out_amount_raw
            price = result.fill_price_usd or (result.value_usd / result.token_qty if result.token_qty else 0.0)
            existing = await repo.open_for_token(TradeMode(order.mode), order.token_mint)
            if existing is not None and existing.status == PositionStatus.OPEN.value:
                total_qty = existing.qty_raw + qty_raw
                existing.entry_price_usd = (
                    ((existing.entry_price_usd * existing.qty_raw + price * qty_raw) / total_qty)
                    if total_qty
                    else price
                )
                existing.qty_raw = total_qty
                existing.initial_qty_raw += qty_raw
                existing.cost_usd += cost
                existing.initial_cost_usd += cost
                existing.fees_usd += result.fees_usd
                existing.at_risk_usd += float(ctx.get("at_risk_usd") or 0.0)
                order.position_id = existing.id
                return None
            pos = await repo.add(
                Position(
                    mode=order.mode,
                    token_mint=order.token_mint,
                    token_symbol=ctx.get("token_symbol"),
                    decimals=int(ctx.get("decimals", 6)),
                    source_wallet_id=ctx.get("source_wallet_id"),
                    entry_signal_id=order.signal_id,
                    exit_mode=ctx.get("exit_mode", ExitMode.PROTECTED.value),
                    status=PositionStatus.OPEN.value,
                    qty_raw=qty_raw,
                    initial_qty_raw=qty_raw,
                    cost_usd=cost,
                    initial_cost_usd=cost,
                    entry_price_usd=price,
                    peak_price_usd=price,
                    last_price_usd=price,
                    last_price_at=now,
                    realized_pnl_usd=0.0,
                    fees_usd=result.fees_usd,
                    tp_levels_hit=[],
                    exit_params=ctx.get("exit_params", {}),
                    exit_seq=0,
                    is_high_risk=bool(ctx.get("is_high_risk")),
                    category=ctx.get("category"),
                    at_risk_usd=float(ctx.get("at_risk_usd") or 0.0),
                    opened_at=now,
                )
            )
            order.position_id = pos.id
            return None

        # ---- exit fill
        found = await repo.get(order.position_id) if order.position_id else None
        if found is None:
            log.error("exit_fill_without_position", order=order.client_order_id)
            return None
        pos = found
        sold = min(result.in_amount_raw, pos.qty_raw)
        portion = sold / pos.qty_raw if pos.qty_raw else 1.0
        cost_portion = pos.cost_usd * portion
        realized = (result.value_usd - result.fees_usd) - cost_portion
        pos.realized_pnl_usd += realized
        pos.cost_usd -= cost_portion
        pos.qty_raw -= sold
        pos.fees_usd += result.fees_usd
        pos.at_risk_usd *= 1 - portion
        if result.fill_price_usd:
            pos.last_price_usd = result.fill_price_usd
            pos.last_price_at = now
        tp = ctx.get("tp_level")
        if tp is not None and tp not in (pos.tp_levels_hit or []):
            pos.tp_levels_hit = [*list(pos.tp_levels_hit or []), tp]
        if pos.qty_raw <= pos.initial_qty_raw * DUST_FRACTION:
            pos.status = PositionStatus.CLOSED.value
            pos.closed_at = now
            pos.close_reason = ctx.get("reason", "cerrada")
            total_return = (pos.realized_pnl_usd / pos.initial_cost_usd * 100) if pos.initial_cost_usd else None
            await EventLogRepo(session).add(
                "positions",
                "position_closed",
                trace_id=order.trace_id,
                data={
                    "position_id": pos.id,
                    "reason": pos.close_reason,
                    "realized_pnl_usd": round(pos.realized_pnl_usd, 4),
                    "return_pct": None if total_return is None else round(total_return, 2),
                },
            )
            self.bus.publish(
                PositionClosed(
                    position_id=pos.id,
                    mode=pos.mode,
                    token_mint=pos.token_mint,
                    token_symbol=pos.token_symbol,
                    reason=pos.close_reason or "",
                    realized_pnl_usd=pos.realized_pnl_usd,
                    return_pct=total_return,
                )
            )
            self._backoff.pop(pos.id, None)
        else:
            pos.status = PositionStatus.OPEN.value
        return realized

    async def on_order_failed(self, order: Order, error: str) -> None:
        if order.purpose != OrderPurpose.EXIT.value or not order.position_id:
            return
        async with self.db.session() as s:
            pos = await PositionRepo(s).get(order.position_id)
            if pos is not None and pos.status == PositionStatus.CLOSING.value:
                pos.status = PositionStatus.OPEN.value
        _, failures = self._backoff.get(order.position_id, (0.0, 0))
        failures += 1
        delay = min(60.0, 2.0**failures)
        self._backoff[order.position_id] = (self.clock.monotonic() + delay, failures)
        if failures == self._config().execution.exit_max_attempts:
            self.bus.publish(
                SystemMessage(
                    title="No se puede cerrar una posición",
                    body=f"Posición #{order.position_id} ({order.token_mint[:8]}…): {failures} intentos fallidos. "
                    f"Último error: {error}. Se seguirá reintentando.",
                    severity=Severity.CRITICAL,
                )
            )

    # ================================================================ exits
    async def exit_position(
        self,
        position_id: int,
        fraction: float,
        trigger: str,
        reason: str,
        *,
        signal_id: int | None = None,
        trace_id: str | None = None,
        tp_level: int | None = None,
        source_price_usd: float | None = None,
    ) -> ExecutionResult | None:
        async with self.db.session() as s:
            pos = await PositionRepo(s).get(position_id)
            mint = pos.token_mint if pos else None
        if mint is None:
            return None
        async with self.locks.hold(mint):
            async with self.db.session() as s:
                pos = await PositionRepo(s).get_for_update(position_id)
                if pos is None or pos.status != PositionStatus.OPEN.value:
                    return None  # closed or an exit is already in flight
                qty = pos.qty_raw if fraction >= 0.999 else int(pos.qty_raw * fraction)
                if qty <= 0:
                    return None
                if trace_id is None and pos.entry_signal_id:
                    # SL/TP/time exits join the entry's trace: one timeline per copied trade
                    entry = await SignalRepo(s).get(pos.entry_signal_id)
                    trace_id = entry.trace_id if entry else None
                pos.exit_seq += 1
                pos.status = PositionStatus.CLOSING.value
                await EventLogRepo(s).add(
                    "positions",
                    "exit_triggered",
                    trace_id=trace_id,
                    data={"position_id": pos.id, "trigger": trigger, "fraction": round(fraction, 4), "reason": reason},
                )
                seq, mode, decimals = pos.exit_seq, TradeMode(pos.mode), pos.decimals
                price = pos.last_price_usd
                ctx: dict[str, Any] = {
                    "reason": reason,
                    "trigger": trigger,
                    "tp_level": tp_level,
                    "token_symbol": pos.token_symbol,
                    "token_mint": pos.token_mint,
                    "source_wallet_id": pos.source_wallet_id,
                    "signal_price_usd": source_price_usd,
                    "theoretical_price_usd": price,
                    "trace_id": trace_id,
                }
            cfg = self._config()
            req = OrderRequest(
                client_order_id=ids.exit_order_id(position_id, trigger, seq),
                purpose=OrderPurpose.EXIT,
                side=Side.SELL,
                mode=mode,
                token_mint=mint,
                token_decimals=decimals,
                input_mint=mint,
                output_mint=cfg.execution.quote_mint,
                amount_in_raw=qty,
                slippage_bps=int(cfg.exits.exit_slippage_pct * 100),
                signal_id=signal_id,
                position_id=position_id,
                theoretical_price_usd=price,
                notional_usd=(qty / 10**decimals * price) if price else None,
                trace_id=trace_id,
            )
            log.info(
                "exit_triggered", position=position_id, trigger=trigger, fraction=round(fraction, 4), reason=reason
            )
            return await self.execution.execute(req, context=ctx, trigger=trigger)

    async def on_source_sell(self, ctx: SignalContext) -> None:
        cfg = self._config()
        async with self.db.session() as s:
            positions = list(await PositionRepo(s).open_from_wallet_token(ctx.wallet.id, ctx.swap.token_mint))
        sold = ctx.swap.sold_fraction
        checks: list[CheckResult] = [
            CheckResult(
                "source_sell",
                "Venta de la wallet origen detectada",
                True,
                None if sold is None else round(sold, 4),
                message=f"vendió {sold * 100:.0f}%" if sold is not None else "",
            )
        ]
        executed = False
        errors: list[str] = []
        for pos in positions:
            fraction = source_sell_fraction(sold, ExitMode(pos.exit_mode), cfg.exits)
            if fraction is None:
                checks.append(
                    CheckResult(
                        f"position_{pos.id}",
                        f"Posición #{pos.id} en modo inteligente",
                        True,
                        message="la salida la gestiona el motor de riesgo",
                        critical=False,
                    )
                )
                continue
            result = await self.exit_position(
                pos.id,
                fraction,
                "source_sell",
                f"La wallet origen vendió {'todo' if fraction >= 0.999 else f'{fraction * 100:.0f}%'}",
                signal_id=ctx.signal_id,
                trace_id=ctx.trace_id,
                source_price_usd=ctx.swap.price_usd,
            )
            ok = bool(result and result.success)
            executed = executed or ok
            if result is not None and not result.success:
                errors.append(result.error or "error")
            checks.append(
                CheckResult(
                    f"position_{pos.id}",
                    f"Salida posición #{pos.id} ({pos.mode})",
                    ok,
                    round(fraction, 4),
                    message=(result.error or "") if result else "sin cambios",
                )
            )
        status = SignalStatus.EXECUTED if executed else (SignalStatus.FAILED if errors else SignalStatus.IGNORED)
        decision = Decision(approved=executed, checks=checks, reason="; ".join(errors) or None)
        async with self.db.session() as s:
            sig = await SignalRepo(s).get(ctx.signal_id)
            if sig is not None:
                sig.status = status.value
                sig.decision = decision.to_dict()
                sig.decided_at = self.clock.now()
                sig.reason = decision.reason or ("Salida espejo ejecutada" if executed else "Sin acción")

    async def close_position(self, position_id: int, *, reason: str = "Cierre manual") -> ExecutionResult | None:
        return await self.exit_position(position_id, 1.0, "manual", reason)

    async def close_all(self, reason: str) -> int:
        async with self.db.session() as s:
            ids_ = [p.id for p in await PositionRepo(s).open_positions()]
        closed = 0
        for pid in ids_:
            result = await self.exit_position(pid, 1.0, "kill_switch", reason)
            closed += bool(result and result.success)
        return closed

    # ============================================================== monitor
    async def check_once(self) -> None:
        cfg = self._config()
        async with self.db.session() as s:
            positions = list(await PositionRepo(s).open_positions())
        for mode in TradeMode:
            metrics.OPEN_POSITIONS.labels(mode=mode.value).set(sum(1 for p in positions if p.mode == mode.value))
        if not positions:
            return
        mints = sorted({p.token_mint for p in positions})
        try:
            prices = await self.tokens.prices(mints, max_age_seconds=cfg.exits.price_poll_seconds)
        except CopyTraderError as exc:
            log.warning("position_prices_failed", error=str(exc))
            prices = {}
        for p in positions:
            if p.token_mint not in prices:
                # No market price (indexer lag, delisted pair...): the executable sell
                # quote is the price that matters for SL/TP anyway.
                quoted = await self._quote_exit_price(p)
                if quoted is not None:
                    prices[p.token_mint] = quoted
        now = self.clock.now()
        to_exit: list[tuple[int, ExitDecision]] = []
        async with self.db.session() as s:
            for p in await PositionRepo(s).open_positions():
                price = prices.get(p.token_mint)
                if price is None:
                    self._check_stale(p, now)
                    continue
                self._stale_alerted.discard(p.id)
                p.last_price_usd = price
                p.last_price_at = now
                p.peak_price_usd = max(p.peak_price_usd, price)
                if p.status != PositionStatus.OPEN.value:
                    continue
                view = PositionView(
                    entry_price_usd=p.entry_price_usd,
                    peak_price_usd=p.peak_price_usd,
                    opened_at=p.opened_at,
                    exit_mode=ExitMode(p.exit_mode),
                    tp_levels_hit=tuple(p.tp_levels_hit or ()),
                )
                decision = evaluate_exit(view, price, now, cfg.exits)
                if decision is not None:
                    to_exit.append((p.id, decision))
        exiting = {pid for pid, _ in to_exit}
        for p in positions:
            if p.id not in exiting and p.status == PositionStatus.OPEN.value:
                missed = await self._missed_source_exit(p)
                if missed is not None:
                    to_exit.append((p.id, missed))
        for pid, decision in to_exit:
            retry_at, _ = self._backoff.get(pid, (0.0, 0))
            if self.clock.monotonic() < retry_at:
                continue
            await self.exit_position(
                pid, decision.fraction, decision.trigger, decision.reason, tp_level=decision.tp_level
            )

    async def _quote_exit_price(self, p: Position) -> float | None:
        """USD price per token implied by a sell quote for the whole position."""
        if p.qty_raw <= 0:
            return None
        cfg = self._config()
        try:
            sol_price = await self.tokens.sol_price()
            if not sol_price:
                return None
            quote = await self.execution.executor(TradeMode(p.mode)).quote(
                p.token_mint, cfg.execution.quote_mint, p.qty_raw, int(cfg.exits.exit_slippage_pct * 100)
            )
        except CopyTraderError as exc:
            log.debug("exit_quote_failed", position=p.id, error=str(exc))
            return None
        tokens = p.qty_raw / 10**p.decimals
        return (quote.out_amount_raw / 10**9 * sol_price) / tokens if tokens > 0 else None

    async def _missed_source_exit(self, p: Position) -> ExitDecision | None:
        """Safety net: the source wallet fully exited but no mirror exit happened.

        Covers the sell arriving while our entry was still pending (the position
        did not exist yet, so the sell was not classified as EXIT), a sell lost
        by the feed and caught up later, or a mirror exit that failed. Only full
        exits are handled here; partial mirrors stay with the normal EXIT flow.
        """
        cfg = self._config()
        if p.source_wallet_id is None or not cfg.signals.follow_sells:
            return None
        if source_sell_fraction(1.0, ExitMode(p.exit_mode), cfg.exits) is None:
            return None  # SMART without close_on_source_sell: the source's exit is irrelevant
        async with self.db.session() as s:
            last = await TransactionRepo(s).last_for_wallet_token(p.source_wallet_id, p.token_mint)
            entry = await SignalRepo(s).get(p.entry_signal_id) if p.entry_signal_id else None
        if last is None or last.side != Side.SELL.value:
            return None
        if entry is not None and entry.source_block_time and last.block_time < entry.source_block_time:
            return None  # an old sell, from before the buy we copied
        if entry is None and last.block_time < p.opened_at:
            return None
        before, after = last.token_balance_before, last.token_balance_after
        if before is None or after is None or before <= 0:
            return None
        if (before - after) / before < cfg.exits.mirror_full_exit_threshold:
            return None
        seen = last.detected_at or last.block_time
        if (self.clock.now() - seen).total_seconds() < SOURCE_EXIT_GRACE_SECONDS:
            return None  # give the regular EXIT signal its chance first
        return ExitDecision(1.0, "source_exited", "La wallet origen ya había vendido todo (salida no espejada)")

    def _check_stale(self, p: Position, now: datetime) -> None:
        ref = p.last_price_at or p.opened_at
        age = (now - ref).total_seconds()
        if age > self._config().exits.stale_price_alert_seconds and p.id not in self._stale_alerted:
            self._stale_alerted.add(p.id)
            self.bus.publish(
                SystemMessage(
                    title="Posición sin precio",
                    body=f"Posición #{p.id} ({p.token_symbol or p.token_mint[:8]}) sin precio desde hace {age:.0f}s: "
                    "stop loss/take profit no pueden evaluarse.",
                    severity=Severity.WARNING,
                )
            )

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.check_once()
            except Exception:
                metrics.ERRORS.labels(component="position_manager").inc()
                log.exception("position_monitor_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self._config().exits.price_poll_seconds)

    async def stop(self) -> None:
        self._stopped.set()
