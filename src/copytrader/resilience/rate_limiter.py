"""Async token-bucket rate limiter (per provider)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    """Allows ``rate`` operations per second with bursts up to ``capacity``."""

    def __init__(self, rate: float, capacity: float | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._clock = clock
        self._updated = clock()
        self._lock = asyncio.Lock()

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

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:  # FIFO fairness between waiters
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                await asyncio.sleep((tokens - self._tokens) / self.rate)

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
