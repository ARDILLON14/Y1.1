"""Repositories for signals, orders, executions, positions and equity."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from copytrader.core.clock import utcnow
from copytrader.core.types import OrderStatus, PositionStatus, TradeMode
from copytrader.db.models import EquitySnapshot, EventLog, Execution, Order, Position, Signal
from copytrader.db.repositories._util import insert_ignore


class SignalRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create(self, values: dict[str, Any]) -> int | None:
        """Idempotent on ``signal_key``; returns None for a duplicate signal."""
        return await insert_ignore(self.s, Signal, values, ["signal_key"])

    async def get(self, signal_id: int) -> Signal | None:
        return await self.s.get(Signal, signal_id)

    async def get_by_key(self, key: str) -> Signal | None:
        return (await self.s.execute(select(Signal).where(Signal.signal_key == key))).scalar_one_or_none()

    async def list(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        action: str | None = None,
        wallet_id: int | None = None,
        before_id: int | None = None,
    ) -> Sequence[Signal]:
        stmt = select(Signal).order_by(Signal.id.desc()).limit(limit)
        if status:
            stmt = stmt.where(Signal.status == status)
        if action:
            stmt = stmt.where(Signal.action == action)
        if wallet_id:
            stmt = stmt.where(Signal.wallet_id == wallet_id)
        if before_id:
            stmt = stmt.where(Signal.id < before_id)
        return (await self.s.execute(stmt)).scalars().all()

    async def counts_since(self, since: datetime) -> dict[str, int]:
        stmt = select(Signal.status, func.count()).where(Signal.created_at >= since).group_by(Signal.status)
        return {row[0]: int(row[1]) for row in (await self.s.execute(stmt)).all()}


class OrderRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create(self, values: dict[str, Any]) -> tuple[Order, bool]:
        """Create an order keyed by ``client_order_id``. Returns (order, created).

        If an order with the same id exists, it is returned untouched — the
        caller must never execute it a second time.
        """
        new_id = await insert_ignore(self.s, Order, values, ["client_order_id"])
        order = await self.get_by_client_id(values["client_order_id"])
        assert order is not None
        if new_id is not None:
            self._timeline(order, "order_created", amount_in_raw=order.amount_in_raw, notional_usd=order.notional_usd)
        return order, new_id is not None

    def _timeline(self, order: Order, event: str, **data: Any) -> None:
        """Per-trace timeline (dashboard: signal detail → "Línea temporal")."""
        self.s.add(
            EventLog(
                component="execution",
                event=event,
                level="warning" if event in ("order_failed", "order_expired") else "info",
                trace_id=order.trace_id,
                data={
                    "order_id": order.id,
                    "client_order_id": order.client_order_id,
                    "purpose": order.purpose,
                    "mode": order.mode,
                    **{k: v for k, v in data.items() if v is not None},
                },
                ts=utcnow(),
            )
        )

    async def get(self, order_id: int) -> Order | None:
        return await self.s.get(Order, order_id)

    async def get_by_client_id(self, client_order_id: str) -> Order | None:
        stmt = select(Order).where(Order.client_order_id == client_order_id)
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def in_flight(self, mode: TradeMode | None = None) -> Sequence[Order]:
        stmt = select(Order).where(
            Order.status.in_(
                [
                    OrderStatus.CREATED.value,
                    OrderStatus.QUOTED.value,
                    OrderStatus.SIGNED.value,
                    OrderStatus.SUBMITTED.value,
                ]
            )
        )
        if mode is not None:
            stmt = stmt.where(Order.mode == mode.value)
        return (await self.s.execute(stmt.order_by(Order.id))).scalars().all()

    async def list(self, *, limit: int = 100, mode: str | None = None) -> Sequence[Order]:
        stmt = select(Order).order_by(Order.id.desc()).limit(limit)
        if mode:
            stmt = stmt.where(Order.mode == mode)
        return (await self.s.execute(stmt)).scalars().all()

    async def set_status(self, order_id: int, status: OrderStatus, *, error: str | None = None, **fields: Any) -> None:
        order = await self.get(order_id)
        if order is None:
            return
        if OrderStatus(order.status).is_terminal and status is not OrderStatus(order.status):
            # Terminal states are final: late callbacks must not resurrect an order.
            return
        changed = order.status != status.value
        order.status = status.value
        order.updated_at = utcnow()
        if error is not None:
            order.error = error[:2000]
        for key, value in fields.items():
            setattr(order, key, value)
        if changed:
            self._timeline(order, f"order_{status.value}", error=error, tx_signature=fields.get("tx_signature"))


class ExecutionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add(self, values: dict[str, Any]) -> int | None:
        return await insert_ignore(self.s, Execution, values, ["order_id"])

    async def list(
        self, *, limit: int = 100, mode: str | None = None, before_id: int | None = None
    ) -> Sequence[tuple[Execution, Order]]:
        stmt = (
            select(Execution, Order)
            .join(Order, Order.id == Execution.order_id)
            .order_by(Execution.id.desc())
            .limit(limit)
        )
        if mode:
            stmt = stmt.where(Execution.mode == mode)
        if before_id:
            stmt = stmt.where(Execution.id < before_id)
        return [(r[0], r[1]) for r in (await self.s.execute(stmt)).all()]

    async def recent_fees(self, mode: str, since: datetime) -> float:
        stmt = select(func.coalesce(func.sum(Execution.fees_usd), 0.0)).where(
            Execution.mode == mode, Execution.executed_at >= since
        )
        return float((await self.s.execute(stmt)).scalar_one())


class PositionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def get(self, position_id: int) -> Position | None:
        return await self.s.get(Position, position_id)

    async def get_for_update(self, position_id: int) -> Position | None:
        stmt = select(Position).where(Position.id == position_id)
        if self.s.get_bind().dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def add(self, position: Position) -> Position:
        self.s.add(position)
        await self.s.flush()
        return position

    async def open_positions(self, mode: TradeMode | None = None) -> Sequence[Position]:
        stmt = select(Position).where(Position.status.in_([PositionStatus.OPEN.value, PositionStatus.CLOSING.value]))
        if mode is not None:
            stmt = stmt.where(Position.mode == mode.value)
        return (await self.s.execute(stmt.order_by(Position.id))).scalars().all()

    async def open_for_token(self, mode: TradeMode, mint: str) -> Position | None:
        stmt = (
            select(Position)
            .where(
                Position.mode == mode.value,
                Position.token_mint == mint,
                Position.status.in_([PositionStatus.OPEN.value, PositionStatus.CLOSING.value]),
            )
            .order_by(Position.id.desc())
            .limit(1)
        )
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def open_from_wallet_token(self, wallet_id: int, mint: str) -> Sequence[Position]:
        stmt = select(Position).where(
            Position.source_wallet_id == wallet_id,
            Position.token_mint == mint,
            Position.status.in_([PositionStatus.OPEN.value, PositionStatus.CLOSING.value]),
        )
        return (await self.s.execute(stmt)).scalars().all()

    async def list(
        self,
        *,
        status: str | None = None,
        mode: str | None = None,
        source_wallet_id: int | None = None,
        limit: int = 200,
    ) -> Sequence[Position]:
        stmt = select(Position).order_by(Position.id.desc()).limit(limit)
        if source_wallet_id is not None:
            stmt = stmt.where(Position.source_wallet_id == source_wallet_id)
        if status == "open":
            stmt = stmt.where(Position.status.in_([PositionStatus.OPEN.value, PositionStatus.CLOSING.value]))
        elif status:
            stmt = stmt.where(Position.status == status)
        if mode:
            stmt = stmt.where(Position.mode == mode)
        return (await self.s.execute(stmt)).scalars().all()

    async def realized_pnl_total(self, mode: TradeMode) -> float:
        stmt = select(func.coalesce(func.sum(Position.realized_pnl_usd), 0.0)).where(Position.mode == mode.value)
        return float((await self.s.execute(stmt)).scalar_one())

    async def closed_since(self, mode: TradeMode, since: datetime) -> Sequence[Position]:
        stmt = (
            select(Position)
            .where(
                Position.mode == mode.value, Position.status == PositionStatus.CLOSED.value, Position.closed_at >= since
            )
            .order_by(Position.closed_at)
        )
        return (await self.s.execute(stmt)).scalars().all()

    async def last_closed(self, mode: TradeMode, limit: int = 50) -> Sequence[Position]:
        stmt = (
            select(Position)
            .where(Position.mode == mode.value, Position.status == PositionStatus.CLOSED.value)
            .order_by(Position.closed_at.desc())
            .limit(limit)
        )
        return (await self.s.execute(stmt)).scalars().all()

    async def last_closed_for_token(self, mode: TradeMode, mint: str) -> Position | None:
        stmt = (
            select(Position)
            .where(
                Position.mode == mode.value, Position.token_mint == mint, Position.status == PositionStatus.CLOSED.value
            )
            .order_by(Position.closed_at.desc())
            .limit(1)
        )
        return (await self.s.execute(stmt)).scalar_one_or_none()


class EquityRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add(self, snap: EquitySnapshot) -> None:
        self.s.add(snap)

    async def first_since(self, mode: TradeMode, since: datetime) -> EquitySnapshot | None:
        stmt = (
            select(EquitySnapshot)
            .where(EquitySnapshot.mode == mode.value, EquitySnapshot.ts >= since)
            .order_by(EquitySnapshot.ts)
            .limit(1)
        )
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def last_before(self, mode: TradeMode, before: datetime) -> EquitySnapshot | None:
        stmt = (
            select(EquitySnapshot)
            .where(EquitySnapshot.mode == mode.value, EquitySnapshot.ts < before)
            .order_by(EquitySnapshot.ts.desc())
            .limit(1)
        )
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def peak(self, mode: TradeMode) -> float | None:
        stmt = select(func.max(EquitySnapshot.equity_usd)).where(EquitySnapshot.mode == mode.value)
        value = (await self.s.execute(stmt)).scalar_one_or_none()
        return float(value) if value is not None else None

    async def series(self, mode: TradeMode, since: datetime, max_points: int = 500) -> list[EquitySnapshot]:
        stmt = (
            select(EquitySnapshot)
            .where(EquitySnapshot.mode == mode.value, EquitySnapshot.ts >= since)
            .order_by(EquitySnapshot.ts)
        )
        rows = list((await self.s.execute(stmt)).scalars().all())
        if len(rows) <= max_points:
            return rows
        step = len(rows) / max_points
        sampled = [rows[int(i * step)] for i in range(max_points)]
        if sampled[-1] is not rows[-1]:
            sampled.append(rows[-1])
        return sampled
