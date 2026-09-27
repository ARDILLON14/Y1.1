"""Kill switches (persisted, survive restarts).

* DAILY  — tripped automatically by the daily loss limit (or manually);
           resets by itself when the UTC day changes.
* GLOBAL — tripped manually or by severe conditions (weekly/monthly loss,
           execution-error bursts, consecutive losses, reconciliation
           mismatch). Only a human can reset it.

Both block NEW entries only. Exits keep working so risk can always be reduced.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import structlog

from copytrader.core.clock import Clock
from copytrader.core.events import EventBus, KillSwitchChanged
from copytrader.core.types import KillSwitchScope
from copytrader.db.base import Database
from copytrader.db.repositories import AuditRepo, RiskEventRepo, SystemStateRepo
from copytrader.observability import metrics

log = structlog.get_logger(__name__)
STATE_KEY = "kill_switches"


class KillSwitchService:
    def __init__(self, db: Database, clock: Clock, bus: EventBus) -> None:
        self.db = db
        self.clock = clock
        self.bus = bus
        self._state: dict[str, dict[str, Any]] = {}
        self.on_global_flatten: Callable[[str], Any] | None = None

    async def load(self) -> None:
        async with self.db.session() as s:
            self._state = await SystemStateRepo(s).get(STATE_KEY) or {}
        self._refresh_metrics()

    def _today(self) -> str:
        return self.clock.now().strftime("%Y-%m-%d")

    def is_active(self, scope: KillSwitchScope) -> bool:
        entry = self._state.get(scope.value) or {}
        if not entry.get("active"):
            return False
        return not (scope is KillSwitchScope.DAILY and entry.get("day") != self._today())

    def last_reset_at(self, scope: KillSwitchScope) -> datetime | None:
        entry = self._state.get(scope.value) or {}
        if entry.get("active") or not entry.get("at"):
            return None
        try:
            return datetime.fromisoformat(entry["at"])
        except ValueError:
            return None

    def blocking_reason(self) -> str | None:
        for scope in (KillSwitchScope.GLOBAL, KillSwitchScope.DAILY):
            if self.is_active(scope):
                entry = self._state[scope.value]
                return f"Kill switch {scope.value.upper()} activo: {entry.get('reason', '')}"
        return None

    def snapshot(self) -> dict[str, Any]:
        return {scope.value: {**(self._state.get(scope.value) or {}), "active": self.is_active(scope)}
                for scope in KillSwitchScope}

    async def activate(self, scope: KillSwitchScope, reason: str, *, actor: str = "system",
                       ip: str | None = None, flatten: bool = False) -> bool:
        if self.is_active(scope):
            return False
        self._state[scope.value] = {"active": True, "reason": reason, "actor": actor,
                                    "at": self.clock.now().isoformat(), "day": self._today()}
        await self._persist(actor, f"kill_switch_on:{scope.value}", reason, ip)
        log.critical("kill_switch_activated", scope=scope.value, reason=reason, actor=actor)
        self.bus.publish(KillSwitchChanged(scope=scope, active=True, reason=reason, actor=actor))
        if flatten and self.on_global_flatten is not None:
            result = self.on_global_flatten(reason)
            if hasattr(result, "__await__"):
                await result
        return True

    async def deactivate(self, scope: KillSwitchScope, *, actor: str, ip: str | None = None) -> bool:
        if not (self._state.get(scope.value) or {}).get("active"):
            return False
        self._state[scope.value] = {"active": False, "reason": "", "actor": actor,
                                    "at": self.clock.now().isoformat()}
        await self._persist(actor, f"kill_switch_off:{scope.value}", "manual reset", ip)
        self.bus.publish(KillSwitchChanged(scope=scope, active=False, reason="reset manual", actor=actor))
        return True

    async def _persist(self, actor: str, action: str, reason: str, ip: str | None) -> None:
        async with self.db.session() as s:
            await SystemStateRepo(s).set(STATE_KEY, self._state)
            await AuditRepo(s).add(actor, action, None, {"reason": reason}, ip)
            await RiskEventRepo(s).add("kill_switch", "critical", f"{action}: {reason}", {"actor": actor})
        self._refresh_metrics()

    def _refresh_metrics(self) -> None:
        for scope in KillSwitchScope:
            metrics.KILL_SWITCH.labels(scope=scope.value).set(1 if self.is_active(scope) else 0)
