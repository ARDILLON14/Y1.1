"""Historical SOL/USD prices (hourly candles) for valuing backfilled swaps and
classifying market regimes.

Default source: Binance public market-data mirror (no API key). Any source
returning ``[[open_time_ms, open, high, low, close, ...], ...]`` works.
"""

from __future__ import annotations

import asyncio
import time
from bisect import bisect_right
from datetime import datetime, timedelta

from copytrader.core.clock import from_unix
from copytrader.resilience.http import ResilientHttp

_HOUR_MS = 3_600_000
_CHUNK = 1000  # candles per request


class KlinesSolPriceHistory:
    def __init__(self, http: ResilientHttp, url: str, symbol: str = "SOLUSDT") -> None:
        self.http = http
        self.url = url
        self.symbol = symbol
        self._closes: dict[int, float] = {}  # hour index -> close
        self._loaded_chunks: set[int] = set()
        self._fetched_at: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def _ensure_chunk(self, chunk: int) -> None:
        if chunk in self._loaded_chunks or time.time() - self._fetched_at.get(chunk, 0.0) < 300:
            return
        async with self._lock:
            if chunk in self._loaded_chunks or time.time() - self._fetched_at.get(chunk, 0.0) < 300:
                return
            start_ms = chunk * _CHUNK * _HOUR_MS
            end_ms = start_ms + _CHUNK * _HOUR_MS - 1
            data = await self.http.get_json(self.url, params={
                "symbol": self.symbol, "interval": "1h", "startTime": start_ms, "endTime": end_ms,
                "limit": _CHUNK})
            for row in data or []:
                try:
                    self._closes[int(row[0]) // _HOUR_MS] = float(row[4])
                except (TypeError, ValueError, IndexError):
                    continue
            self._fetched_at[chunk] = time.time()
            # The current chunk keeps growing: only cache chunks fully in the past.
            if end_ms < time.time() * 1000 - _HOUR_MS:
                self._loaded_chunks.add(chunk)

    async def sol_price_at(self, ts: datetime) -> float | None:
        hour = int(ts.timestamp() * 1000) // _HOUR_MS
        await self._ensure_chunk(hour // _CHUNK)
        for h in (hour, hour - 1, hour - 2):
            if h in self._closes:
                return self._closes[h]
        return None

    async def sol_series(self, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        first = int(start.timestamp() * 1000) // _HOUR_MS
        last = int(end.timestamp() * 1000) // _HOUR_MS
        for chunk in range(first // _CHUNK, last // _CHUNK + 1):
            await self._ensure_chunk(chunk)
        return [(from_unix(h * 3600), self._closes[h]) for h in range(first, last + 1) if h in self._closes]


class StaticSeriesSolPriceHistory:
    """In-memory series (simulation, backtests, tests)."""

    def __init__(self, series: list[tuple[datetime, float]] | None = None,
                 fallback: float | None = None) -> None:
        self._times: list[datetime] = []
        self._values: list[float] = []
        self.fallback = fallback
        for ts, value in sorted(series or []):
            self.add(ts, value)

    def add(self, ts: datetime, value: float) -> None:
        if self._times and ts <= self._times[-1]:
            idx = bisect_right(self._times, ts)
            self._times.insert(idx, ts)
            self._values.insert(idx, value)
        else:
            self._times.append(ts)
            self._values.append(value)

    async def sol_price_at(self, ts: datetime) -> float | None:
        idx = bisect_right(self._times, ts) - 1
        if idx < 0:
            return self._values[0] if self._values else self.fallback
        if ts - self._times[idx] > timedelta(days=2):
            return self.fallback if self.fallback is not None else self._values[idx]
        return self._values[idx]

    async def sol_series(self, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        return [(t, v) for t, v in zip(self._times, self._values, strict=True) if start <= t <= end]
