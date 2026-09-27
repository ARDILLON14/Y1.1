"""Registry of component health, consumed by the API, preflight and alerts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from copytrader.core.clock import utcnow


class HealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ComponentHealth:
    name: str
    kind: str
    status: HealthStatus = HealthStatus.UNKNOWN
    detail: str = ""
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status.value,
            "detail": self.detail,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "last_error": self.last_error,
            "extra": self.extra,
        }


class HealthRegistry:
    def __init__(self) -> None:
        self._items: dict[str, ComponentHealth] = {}

    def _get(self, name: str, kind: str) -> ComponentHealth:
        item = self._items.get(name)
        if item is None:
            item = ComponentHealth(name=name, kind=kind)
            self._items[name] = item
        return item

    def ok(self, name: str, kind: str = "component", detail: str = "", **extra: Any) -> bool:
        """Mark healthy. Returns True if this is a transition from not-OK."""
        item = self._get(name, kind)
        changed = item.status is not HealthStatus.OK
        item.status = HealthStatus.OK
        item.detail = detail
        item.last_ok_at = utcnow()
        item.extra.update(extra)
        return changed

    def fail(self, name: str, kind: str = "component", error: str = "",
             status: HealthStatus = HealthStatus.DOWN, **extra: Any) -> bool:
        """Mark unhealthy. Returns True if this is a transition from OK/unknown."""
        item = self._get(name, kind)
        changed = item.status is not status
        item.status = status
        item.detail = error
        item.last_error = error
        item.last_error_at = utcnow()
        item.extra.update(extra)
        return changed

    def get(self, name: str) -> ComponentHealth | None:
        return self._items.get(name)

    def is_ok(self, name: str) -> bool:
        item = self._items.get(name)
        return bool(item and item.status is HealthStatus.OK)

    def snapshot(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in sorted(self._items.values(), key=lambda i: i.name)]

    def overall(self) -> HealthStatus:
        statuses = {i.status for i in self._items.values()}
        if HealthStatus.DOWN in statuses:
            return HealthStatus.DOWN
        if HealthStatus.DEGRADED in statuses or HealthStatus.UNKNOWN in statuses:
            return HealthStatus.DEGRADED
        return HealthStatus.OK if statuses else HealthStatus.UNKNOWN
