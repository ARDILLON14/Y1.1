"""Injectable clock so time-dependent logic is deterministic in tests/backtests."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime (naive values are assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def from_unix(ts: float | int) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """Clock controlled by tests and the backtester."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = ensure_utc(start or datetime(2025, 1, 1, tzinfo=UTC))
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def set(self, value: datetime) -> None:
        new = ensure_utc(value)
        self._mono += max(0.0, (new - self._now).total_seconds())
        self._now = new

    def advance(self, seconds: float = 0.0, **kwargs: float) -> datetime:
        delta = timedelta(seconds=seconds, **kwargs)
        self._now += delta
        self._mono += delta.total_seconds()
        return self._now
