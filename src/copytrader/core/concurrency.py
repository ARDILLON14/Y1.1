"""Concurrency helpers."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Hashable


class KeyedLocks:
    """One ``asyncio.Lock`` per key, cleaned up when nobody holds or waits on it.

    Used to serialise work per token and per position so that, e.g., a stop-loss
    and a mirrored sell can never close the same position twice.
    """

    def __init__(self) -> None:
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._refs: dict[Hashable, int] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: Hashable) -> AsyncIterator[None]:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._refs[key] = self._refs.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._refs[key] -= 1
            if self._refs[key] == 0:
                del self._refs[key]
                del self._locks[key]

    def locked(self, key: Hashable) -> bool:
        lock = self._locks.get(key)
        return bool(lock and lock.locked())


class LRUSet:
    """Bounded set remembering the most recent ``maxsize`` keys (in-memory dedupe)."""

    def __init__(self, maxsize: int = 50_000) -> None:
        self._data: OrderedDict[Hashable, None] = OrderedDict()
        self._maxsize = maxsize

    def add(self, key: Hashable) -> bool:
        """Add ``key``; return ``False`` if it was already present."""
        if key in self._data:
            self._data.move_to_end(key)
            return False
        self._data[key] = None
        if len(self._data) > self._maxsize:
            self._data.popitem(last=False)
        return True

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)


class TTLCache[K: Hashable, V]:
    """Tiny in-process TTL cache (monotonic time), bounded in size."""

    def __init__(self, ttl_seconds: float, maxsize: int = 10_000) -> None:
        self._ttl = ttl_seconds
        self._maxsize = maxsize
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K, now: float | None = None) -> V | None:
        now = time.monotonic() if now is None else now
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < now:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: K, value: V, now: float | None = None, ttl: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._data[key] = (now + (self._ttl if ttl is None else ttl), value)
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def invalidate(self, key: K) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()
