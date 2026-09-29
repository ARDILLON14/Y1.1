"""Cached historical candles and token snapshots for the backtester."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from copytrader.db.models import PriceCandle, PriceFetch, TokenSnapshot
from copytrader.db.repositories._util import insert_many_ignore


class PriceHistoryRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def candles(
        self, mints: Iterable[str], interval_minutes: int, start: datetime, end: datetime
    ) -> Sequence[PriceCandle]:
        wanted = sorted(set(mints))
        if not wanted:
            return []
        stmt = (
            select(PriceCandle)
            .where(
                PriceCandle.mint.in_(wanted),
                PriceCandle.interval_minutes == interval_minutes,
                PriceCandle.ts >= start,
                PriceCandle.ts <= end,
            )
            .order_by(PriceCandle.mint, PriceCandle.ts)
        )
        return (await self.s.execute(stmt)).scalars().all()

    async def add_candles(self, rows: list[dict[str, object]]) -> int:
        return await insert_many_ignore(self.s, PriceCandle, rows, ["mint", "interval_minutes", "ts"])

    async def fetches(self, mints: Iterable[str], interval_minutes: int) -> Sequence[PriceFetch]:
        wanted = sorted(set(mints))
        if not wanted:
            return []
        stmt = select(PriceFetch).where(PriceFetch.mint.in_(wanted), PriceFetch.interval_minutes == interval_minutes)
        return (await self.s.execute(stmt)).scalars().all()

    async def record_fetch(
        self,
        *,
        mint: str,
        interval_minutes: int,
        start: datetime,
        end: datetime,
        source: str,
        pool: str | None,
        candles: int,
        error: str | None,
        fetched_at: datetime,
    ) -> None:
        self.s.add(
            PriceFetch(
                mint=mint,
                interval_minutes=interval_minutes,
                start=start,
                end=end,
                source=source,
                pool=pool,
                candles=candles,
                error=error[:500] if error else None,
                fetched_at=fetched_at,
            )
        )

    async def snapshots(self, mints: Iterable[str], start: datetime, end: datetime) -> Sequence[TokenSnapshot]:
        """What the bot itself observed of these tokens while running (price, liquidity)."""
        wanted = sorted(set(mints))
        if not wanted:
            return []
        stmt = (
            select(TokenSnapshot)
            .where(TokenSnapshot.mint.in_(wanted), TokenSnapshot.ts >= start, TokenSnapshot.ts <= end)
            .order_by(TokenSnapshot.mint, TokenSnapshot.ts)
        )
        return (await self.s.execute(stmt)).scalars().all()
