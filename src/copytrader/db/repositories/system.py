"""Repositories for system state, alerts, audit/event logs, config and users."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from copytrader.config.service import ConfigVersion
from copytrader.core.clock import utcnow
from copytrader.db.base import Database
from copytrader.db.models import (
    Alert,
    AuditLog,
    BacktestRun,
    ConfigVersionRow,
    EventLog,
    RiskEvent,
    SystemState,
    User,
)
from copytrader.db.repositories._util import insert_ignore


class SystemStateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def get(self, key: str) -> dict[str, Any] | None:
        row = await self.s.get(SystemState, key)
        return dict(row.value) if row else None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        row = await self.s.get(SystemState, key)
        if row is None:
            self.s.add(SystemState(key=key, value=value, updated_at=utcnow()))
        else:
            row.value = value
            row.updated_at = utcnow()


class RiskEventRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add(self, type_: str, severity: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.s.add(RiskEvent(type=type_, severity=severity, message=message, data=data or {}, ts=utcnow()))

    async def recent(self, limit: int = 50) -> Sequence[RiskEvent]:
        stmt = select(RiskEvent).order_by(RiskEvent.id.desc()).limit(limit)
        return (await self.s.execute(stmt)).scalars().all()


class AlertRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add(self, alert: Alert) -> Alert:
        self.s.add(alert)
        await self.s.flush()
        return alert

    async def list(
        self,
        *,
        limit: int = 100,
        type_: str | None = None,
        severity: str | None = None,
        unacknowledged: bool = False,
        before_id: int | None = None,
    ) -> Sequence[Alert]:
        stmt = select(Alert).order_by(Alert.id.desc()).limit(limit)
        if type_:
            stmt = stmt.where(Alert.type == type_)
        if severity:
            stmt = stmt.where(Alert.severity == severity)
        if unacknowledged:
            stmt = stmt.where(Alert.acknowledged.is_(False))
        if before_id:
            stmt = stmt.where(Alert.id < before_id)
        return (await self.s.execute(stmt)).scalars().all()

    async def acknowledge(self, alert_id: int | None = None) -> None:
        stmt = select(Alert).where(Alert.acknowledged.is_(False))
        if alert_id is not None:
            stmt = stmt.where(Alert.id == alert_id)
        for row in (await self.s.execute(stmt)).scalars().all():
            row.acknowledged = True


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add(
        self,
        actor: str,
        action: str,
        target: str | None = None,
        data: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        self.s.add(AuditLog(actor=actor, action=action, target=target, data=data or {}, ip=ip, ts=utcnow()))

    async def list(self, limit: int = 200) -> Sequence[AuditLog]:
        return (await self.s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))).scalars().all()


class EventLogRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add(
        self,
        component: str,
        event: str,
        *,
        level: str = "info",
        trace_id: str | None = None,
        data: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> None:
        self.s.add(
            EventLog(
                component=component, event=event, level=level, trace_id=trace_id, data=data or {}, ts=ts or utcnow()
            )
        )

    async def by_trace(self, trace_id: str) -> Sequence[EventLog]:
        stmt = select(EventLog).where(EventLog.trace_id == trace_id).order_by(EventLog.id)
        return (await self.s.execute(stmt)).scalars().all()

    async def purge_before(self, before: datetime) -> None:
        await self.s.execute(delete(EventLog).where(EventLog.ts < before))


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def get(self, username: str) -> User | None:
        return (await self.s.execute(select(User).where(User.username == username))).scalar_one_or_none()

    async def any_user(self) -> bool:
        return (await self.s.execute(select(User.id).limit(1))).scalar_one_or_none() is not None

    async def set_password(self, username: str, password_hash: str) -> None:
        user = await self.get(username)
        if user is None:
            self.s.add(User(username=username, password_hash=password_hash))
        else:
            user.password_hash = password_hash
            user.password_changed_at = utcnow()

    async def set_totp(self, username: str, secret_enc: str | None) -> None:
        user = await self.get(username)
        if user is not None:
            user.totp_secret_enc = secret_enc


class BacktestRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create(self, params: dict[str, Any]) -> BacktestRun:
        run = BacktestRun(params=params, status="running")
        self.s.add(run)
        await self.s.flush()
        return run

    async def get(self, run_id: int) -> BacktestRun | None:
        return await self.s.get(BacktestRun, run_id)

    async def list(self, limit: int = 20) -> Sequence[BacktestRun]:
        stmt = select(BacktestRun).order_by(BacktestRun.id.desc()).limit(limit)
        return (await self.s.execute(stmt)).scalars().all()


class DbConfigStore:
    """``ConfigStore`` implementation backed by ``config_versions``."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def load_latest(self) -> ConfigVersion | None:
        async with self.db.session() as s:
            row = (
                await s.execute(select(ConfigVersionRow).order_by(ConfigVersionRow.version.desc()).limit(1))
            ).scalar_one_or_none()
            return _to_version(row) if row else None

    async def save(self, version: ConfigVersion) -> None:
        async with self.db.session() as s:
            new_id = await insert_ignore(
                s,
                ConfigVersionRow,
                {
                    "version": version.version,
                    "overrides": version.overrides,
                    "author": version.author,
                    "comment": version.comment,
                    "created_at": utcnow(),
                },
                ["version"],
            )
            if new_id is None:
                raise RuntimeError(f"config version {version.version} already exists (concurrent edit?)")

    async def get(self, version: int) -> ConfigVersion | None:
        async with self.db.session() as s:
            row = (
                await s.execute(select(ConfigVersionRow).where(ConfigVersionRow.version == version))
            ).scalar_one_or_none()
            return _to_version(row) if row else None

    async def history(self, limit: int = 50) -> list[ConfigVersion]:
        async with self.db.session() as s:
            rows = (
                (await s.execute(select(ConfigVersionRow).order_by(ConfigVersionRow.version.desc()).limit(limit)))
                .scalars()
                .all()
            )
            return [_to_version(r) for r in rows]


def _to_version(row: ConfigVersionRow) -> ConfigVersion:
    return ConfigVersion(
        version=row.version,
        overrides=dict(row.overrides),
        author=row.author,
        comment=row.comment,
        created_at=row.created_at,
    )
