"""GeckoTerminal public API: the main pool of a token and its historical OHLCV candles.

Used only by the backtester, to evaluate stop loss, take profit, trailing and
time exits BETWEEN the trades of the copied wallets (their swaps alone say
nothing about what the price did in between). No API key; the public API allows
~30 requests per minute, so candles are cached in the database and fetched once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from copytrader.resilience.http import ResilientHttp
from copytrader.resilience.rate_limiter import Priority

PAGE_LIMIT = 1000
MAX_PAGES = 20
MINUTE_AGGREGATES = (1, 5, 15)
HOUR_AGGREGATES = (1, 4, 12)


@dataclass(frozen=True, slots=True)
class Candle:
    ts: datetime  # start of the candle (UTC)
    open: float
    high: float
    low: float
    close: float
    volume_usd: float | None = None


def timeframe_for(minutes: int) -> tuple[str, int]:
    """GeckoTerminal (timeframe, aggregate) for a candle length in minutes."""
    if minutes in MINUTE_AGGREGATES:
        return "minute", minutes
    if minutes % 60 == 0 and minutes // 60 in HOUR_AGGREGATES:
        return "hour", minutes // 60
    raise ValueError(f"unsupported candle length: {minutes} min")


def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f >= 0 else None


def parse_ohlcv(data: Any) -> list[Candle]:
    """Candles of an OHLCV response (any order in, oldest first out); malformed rows are skipped."""
    rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out: list[Candle] = []
    for row in rows:
        if not isinstance(row, list | tuple) or len(row) < 5:
            continue
        values = [_num(v) for v in row[1:5]]
        ts = _num(row[0])
        if ts is None or any(v is None or v <= 0 for v in values):
            continue
        o, h, lo, c = (float(v) for v in values if v is not None)
        out.append(
            Candle(
                datetime.fromtimestamp(ts, tz=UTC),
                o,
                max(h, o, c),
                min(lo, o, c),
                c,
                _num(row[5]) if len(row) > 5 else None,
            )
        )
    return sorted(out, key=lambda c: c.ts)


class GeckoTerminalClient:
    source = "geckoterminal"

    def __init__(self, http: ResilientHttp, base_url: str, network: str = "solana") -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.network = network

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        return await self.http.get_json(
            f"{self.base_url}/networks/{self.network}/{path}",
            params=params,
            headers={"accept": "application/json"},
            priority=Priority.BACKGROUND,
        )

    async def top_pool(self, mint: str) -> str | None:
        """Address of the token's deepest pool (highest reserve in USD)."""
        data = await self._get(f"tokens/{mint}/pools", {"page": "1"})
        best: tuple[float, str] | None = None
        for pool in (data or {}).get("data") or []:
            attrs = (pool or {}).get("attributes") or {}
            address = attrs.get("address")
            if not address:
                continue
            reserve = _num(attrs.get("reserve_in_usd")) or 0.0
            if best is None or reserve > best[0]:
                best = (reserve, str(address))
        return best[1] if best else None

    async def candles(self, pool: str, mint: str, start: datetime, end: datetime, minutes: int) -> list[Candle]:
        """USD candles of ``mint`` in ``pool`` covering [start, end], oldest first."""
        timeframe, aggregate = timeframe_for(minutes)
        before = int(end.timestamp())
        found: dict[datetime, Candle] = {}
        for _ in range(MAX_PAGES):
            data = await self._get(
                f"pools/{pool}/ohlcv/{timeframe}",
                {
                    "aggregate": str(aggregate),
                    "before_timestamp": str(before),
                    "limit": str(PAGE_LIMIT),
                    "currency": "usd",
                    "token": mint,  # price of OUR token, whichever side of the pool it is
                },
            )
            page = parse_ohlcv(data)
            if not page:
                break
            for candle in page:
                if start <= candle.ts <= end:
                    found[candle.ts] = candle
            oldest = page[0].ts
            if len(page) < PAGE_LIMIT or oldest <= start or int(oldest.timestamp()) >= before:
                break
            before = int(oldest.timestamp())
        return [found[ts] for ts in sorted(found)]
