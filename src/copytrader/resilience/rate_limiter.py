"""Async token-bucket rate limiter (per provider)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from enum import IntEnum

_BLOCKED_POLL_SECONDS = 0.02


class Priority(IntEnum):
    """Who is served first when a provider's budget is exhausted (lower = first)."""

    EXECUTION = 0  # quotes and swaps of orders being executed (entries and exits)
    NORMAL = 1
    BACKGROUND = 2  # price polling, fallback quotes: may always wait


class TokenBucket:
    """Allows ``rate`` operations per second with bursts up to ``capacity``.

    Waiters are served by priority, FIFO within the same priority: a burst of
    background price polling can never delay the quote of an order.
    """

    def __init__(self, rate: float, capacity: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._clock = clock
        self._updated = clock()
        self._locks: dict[int, asyncio.Lock] = {}
        self._waiting: dict[int, int] = {}

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    def _higher_priority_waiting(self, priority: int) -> bool:
        return any(count > 0 for p, count in self._waiting.items() if p < priority)

    async def acquire(self, tokens: float = 1.0, *, priority: int = Priority.NORMAL) -> None:
        lock = self._locks.setdefault(priority, asyncio.Lock())
        self._waiting[priority] = self._waiting.get(priority, 0) + 1
        try:
            async with lock:  # FIFO fairness within the same priority
                while True:
                    self._refill()
                    blocked = self._higher_priority_waiting(priority)
                    if self._tokens >= tokens and not blocked:
                        self._tokens -= tokens
                        return
                    missing = max(0.0, tokens - self._tokens) / self.rate
                    await asyncio.sleep(max(missing, _BLOCKED_POLL_SECONDS) if blocked else missing)
        finally:
            self._waiting[priority] -= 1

    def penalize(self, seconds: float) -> None:
        """After a 429, drain the bucket so the next calls wait ``seconds``."""
        self._refill()
        self._tokens = min(self._tokens, -seconds * self.rate)


class SlidingWindowCounter:
    """Counts events in a trailing window (used for 'N errors per hour' rules)."""

    def __init__(self, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.window = window_seconds
        self._clock = clock
        self._events: list[float] = []

    def add(self) -> int:
        now = self._clock()
        self._events.append(now)
        return self.count(now)

    def count(self, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        cutoff = now - self.window
        self._events = [t for t in self._events if t > cutoff]
        return len(self._events)
