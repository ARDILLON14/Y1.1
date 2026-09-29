"""Historical prices for the backtester (real data).

With live data the backtester only knew the prices at which the wallets
traded, so stop loss, take profit, trailing and time exits could not happen
between two trades. Here the price path comes from:

* **candles** (OHLCV) of each token's main pool, downloaded from GeckoTerminal
  once and cached in the database (``price_candles``), with every attempt
  logged (``price_fetches``) so tokens without history are not asked for again
  until ``backtest.refetch_failed_after_hours``;
* **the bot's own snapshots** (``token_snapshots``: price and liquidity it saw
  while running), used for pool liquidity and as a fallback price.

``price_at`` never looks ahead: it returns the close of the last candle that
had already ENDED at that moment.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from copytrader.core.errors import CopyTraderError, ProviderError
from copytrader.core.models import SwapEvent
from copytrader.core.types import Side
from copytrader.db.base import Database
from copytrader.db.repositories import PriceHistoryRepo
from copytrader.providers.geckoterminal import Candle, GeckoTerminalClient

log = structlog.get_logger(__name__)

LOOKBACK = timedelta(days=1)  # before the first buy: the volatility estimate needs 24 h of prices
HOLD_AFTER = timedelta(days=3)  # after the last trade: positions may stay open until their time limit
MAX_PRICE_STALENESS = timedelta(hours=12)  # tokens without trades have no candles: last close stays valid
MAX_SNAPSHOT_STALENESS = timedelta(hours=2)

Progress = Callable[[int, int], Awaitable[None]]


@dataclass
class _Series:
    ts: list[float] = field(default_factory=list)  # epoch seconds (candle start / snapshot time)
    values: list[tuple[float, ...]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PriceNeed:
    mint: str
    start: datetime
    end: datetime
    buys: int


class PriceHistory:
    def __init__(self, candle_minutes: int) -> None:
        self.candle_minutes = candle_minutes
        self._step = candle_minutes * 60
        self._candles: dict[str, _Series] = {}
        self._snapshots: dict[str, _Series] = {}  # (price, liquidity)
        self.stats: dict[str, Any] = {}

    # ------------------------------------------------------------- loading
    def add_candles(self, mint: str, candles: Iterable[Candle]) -> None:
        series = self._candles.setdefault(mint, _Series())
        merged = dict(zip(series.ts, series.values, strict=True))
        for c in candles:
            merged[c.ts.timestamp()] = (c.open, c.high, c.low, c.close)
        series.ts = sorted(merged)
        series.values = [merged[t] for t in series.ts]

    def add_snapshot(self, mint: str, ts: datetime, price: float | None, liquidity: float | None) -> None:
        series = self._snapshots.setdefault(mint, _Series())
        series.ts.append(ts.timestamp())
        series.values.append((price or 0.0, liquidity or 0.0))

    def has_candles(self, mint: str) -> bool:
        return bool(self._candles.get(mint) and self._candles[mint].ts)

    @property
    def mints_with_candles(self) -> set[str]:
        return {m for m, s in self._candles.items() if s.ts}

    # ------------------------------------------------------------- queries
    def price_at(self, mint: str, t: datetime) -> float | None:
        """Last price known at ``t``: close of the last candle already ended (no look-ahead)."""
        now = t.timestamp()
        series = self._candles.get(mint)
        if series and series.ts:
            i = bisect_right(series.ts, now - self._step) - 1
            if i >= 0 and now - (series.ts[i] + self._step) <= MAX_PRICE_STALENESS.total_seconds():
                return series.values[i][3]
        snap = self._last_snapshot(mint, now)
        return snap[0] if snap and snap[0] > 0 else None

    def ohlc_between(self, mint: str, t0: datetime, t1: datetime) -> tuple[float, float, float, float] | None:
        """(open, high, low, close) of the candles that started at/after ``t0`` and ended by ``t1``."""
        series = self._candles.get(mint)
        if not series or not series.ts:
            return None
        lo = bisect_right(series.ts, t0.timestamp() - 1e-6)
        hi = bisect_right(series.ts, t1.timestamp() - self._step)
        if hi <= lo:
            return None
        rows = series.values[lo:hi]
        return rows[0][0], max(r[1] for r in rows), min(r[2] for r in rows), rows[-1][3]

    def liquidity_at(self, mint: str, t: datetime) -> float | None:
        """Pool liquidity the bot itself observed at or before ``t`` (recent enough)."""
        snap = self._last_snapshot(mint, t.timestamp())
        return snap[1] if snap and snap[1] > 0 else None

    def _last_snapshot(self, mint: str, now: float) -> tuple[float, ...] | None:
        series = self._snapshots.get(mint)
        if not series or not series.ts:
            return None
        i = bisect_right(series.ts, now) - 1
        if i < 0 or now - series.ts[i] > MAX_SNAPSHOT_STALENESS.total_seconds():
            return None
        return series.values[i]


def price_needs(
    swaps: dict[str, list[SwapEvent]], start: datetime, end: datetime, now: datetime, limit: int
) -> tuple[list[PriceNeed], int]:
    """Tokens bought during the evaluated period and the time range each one needs, most-bought
    first, at most ``limit``. Returns (needs, number of tokens that would need prices)."""
    buys: dict[str, int] = defaultdict(int)
    first: dict[str, datetime] = {}
    last: dict[str, datetime] = {}
    for events in swaps.values():
        for ev in events:
            if not start <= ev.block_time <= end:
                continue
            if ev.side is Side.BUY:
                buys[ev.token_mint] += 1
                first[ev.token_mint] = min(first.get(ev.token_mint, ev.block_time), ev.block_time)
            last[ev.token_mint] = max(last.get(ev.token_mint, ev.block_time), ev.block_time)
    ranked = sorted(buys, key=lambda m: (-buys[m], m))
    needs = [
        PriceNeed(m, first[m] - LOOKBACK, min(last[m] + HOLD_AFTER, end + HOLD_AFTER, now), buys[m])
        for m in ranked[:limit]
    ]
    return needs, len(ranked)


async def load_price_history(
    db: Database,
    client: GeckoTerminalClient | None,
    needs: list[PriceNeed],
    candle_minutes: int,
    *,
    now: datetime,
    refetch_failed_after: timedelta,
    progress: Progress | None = None,
) -> PriceHistory:
    """Cached candles + downloads of the missing ranges (when ``client`` is given) + snapshots."""
    history = PriceHistory(candle_minutes)
    if not needs:
        history.stats = {"tokens_considered": 0, "tokens_with_prices": 0, "fetched_now": 0, "failed": 0}
        return history
    step = timedelta(minutes=candle_minutes)
    mints = [n.mint for n in needs]
    lo, hi = min(n.start for n in needs), max(n.end for n in needs)
    async with db.session() as s:
        repo = PriceHistoryRepo(s)
        cached = await repo.candles(mints, candle_minutes, lo, hi)
        attempts = await repo.fetches(mints, candle_minutes)
        snapshots = await repo.snapshots(mints, lo, hi)
    by_mint: dict[str, list[Candle]] = defaultdict(list)
    for row in cached:
        by_mint[row.mint].append(Candle(row.ts, row.open, row.high, row.low, row.close, row.volume_usd))
    for mint, rows in by_mint.items():
        history.add_candles(mint, rows)
    for snap in snapshots:
        history.add_snapshot(snap.mint, snap.ts, snap.price_usd, snap.liquidity_usd)

    todo: list[tuple[PriceNeed, datetime, str | None]] = []  # (need, fetch from, known pool)
    for need in needs:
        tries = [a for a in attempts if a.mint == need.mint]
        ok = [a for a in tries if a.error is None and a.candles > 0 and a.start <= need.start + step]
        covered_until = max((a.end for a in ok), default=None)
        if covered_until is not None and covered_until >= need.end - step:
            continue
        recent_miss = any(
            (a.error is not None or a.candles == 0) and now - a.fetched_at < refetch_failed_after for a in tries
        )
        if recent_miss and covered_until is None:
            continue  # no history there a moment ago: do not ask again yet
        pool = next((a.pool for a in sorted(tries, key=lambda a: a.fetched_at, reverse=True) if a.pool), None)
        todo.append((need, (covered_until - step) if covered_until else need.start, pool))

    fetched = failed = 0
    if client is not None:
        for i, (need, since, pool) in enumerate(todo):
            error: str | None = None
            candles: list[Candle] = []
            try:
                pool = pool or await client.top_pool(need.mint)
                if pool is None:
                    error = "sin pool en GeckoTerminal"
                else:
                    candles = await client.candles(pool, need.mint, since, need.end, candle_minutes)
            except ProviderError as exc:
                if exc.status_code != 404:
                    failed += 1  # transient (rate limit, network): not logged, retried next time
                    log.warning("price_history_fetch_failed", mint=need.mint, error=str(exc))
                    continue
                error = "token no encontrado en GeckoTerminal"
            except CopyTraderError as exc:
                failed += 1
                log.warning("price_history_fetch_failed", mint=need.mint, error=str(exc))
                continue
            async with db.session() as s:
                repo = PriceHistoryRepo(s)
                await repo.add_candles(
                    [
                        {
                            "mint": need.mint,
                            "interval_minutes": candle_minutes,
                            "ts": c.ts,
                            "open": c.open,
                            "high": c.high,
                            "low": c.low,
                            "close": c.close,
                            "volume_usd": c.volume_usd,
                            "source": client.source,
                        }
                        for c in candles
                    ]
                )
                await repo.record_fetch(
                    mint=need.mint,
                    interval_minutes=candle_minutes,
                    start=since,
                    end=need.end,
                    source=client.source,
                    pool=pool,
                    candles=len(candles),
                    error=error if not candles else None,
                    fetched_at=now,
                )
            history.add_candles(need.mint, candles)
            fetched += 1
            if progress is not None:
                await progress(i + 1, len(todo))
    history.stats = {
        "tokens_considered": len(needs),
        "tokens_with_prices": len(history.mints_with_candles & set(mints)),
        "fetched_now": fetched,
        "failed": failed,
        "pending": 0 if client is not None else len(todo),
    }
    return history
