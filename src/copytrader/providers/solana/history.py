"""Historical swaps via ``getSignaturesForAddress`` + ``getTransaction``.

Used for the initial backfill of each wallet and for catch-up after a
WebSocket disconnection (fetch everything after the last signature we saw).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import structlog

from copytrader.core.clock import Clock, SystemClock, from_unix
from copytrader.core.errors import CopyTraderError
from copytrader.core.models import SwapEvent
from copytrader.core.types import TxSource
from copytrader.providers.interfaces import SolPriceHistory
from copytrader.providers.solana.parser import parse_swaps
from copytrader.providers.solana.rpc import SolanaRpc

log = structlog.get_logger(__name__)


class RpcHistorySource:
    def __init__(
        self,
        rpc: SolanaRpc,
        sol_prices: SolPriceHistory,
        *,
        quote_mints: list[str],
        concurrency: int = 4,
        clock: Clock | None = None,
    ) -> None:
        self.rpc = rpc
        self.sol_prices = sol_prices
        self.quote_mints = quote_mints
        self._sem = asyncio.Semaphore(concurrency)
        self.clock = clock or SystemClock()

    async def list_signatures(
        self, wallet: str, *, since: datetime | None, until_signature: str | None, max_signatures: int
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        before: str | None = None
        while len(collected) < max_signatures:
            limit = min(1000, max_signatures - len(collected))
            batch = await self.rpc.get_signatures_for_address(wallet, before=before, until=until_signature, limit=limit)
            if not batch:
                break
            stop = False
            for item in batch:
                bt = item.get("blockTime")
                if since is not None and bt is not None and from_unix(bt) < since:
                    stop = True
                    break
                if item.get("err") is None:
                    collected.append(item)
            before = batch[-1]["signature"]
            if stop or len(batch) < limit:
                break
        return collected

    async def fetch_swaps(
        self,
        wallet: str,
        *,
        since: datetime | None = None,
        until_signature: str | None = None,
        max_signatures: int = 1000,
        source: TxSource = TxSource.BACKFILL,
    ) -> list[SwapEvent]:
        sigs = await self.list_signatures(
            wallet, since=since, until_signature=until_signature, max_signatures=max_signatures
        )
        now = self.clock.now()

        async def one(item: dict[str, Any]) -> list[SwapEvent]:
            async with self._sem:
                try:
                    tx = await self.rpc.get_transaction(item["signature"])
                except CopyTraderError as exc:
                    log.warning("history_tx_fetch_failed", wallet=wallet, error=str(exc))
                    return []
            if not tx:
                return []
            bt = tx.get("blockTime")
            sol_price = await self.sol_prices.sol_price_at(from_unix(bt)) if bt else None
            try:
                return parse_swaps(
                    tx,
                    wallet,
                    sol_price_usd=sol_price,
                    quote_mints=self.quote_mints,
                    source=source,
                    detected_at=now if source is TxSource.CATCHUP else None,
                )
            except CopyTraderError as exc:
                log.warning("history_parse_failed", wallet=wallet, signature=item["signature"], error=str(exc))
                return []

        results = await asyncio.gather(*(one(item) for item in sigs))
        swaps = [s for group in results for s in group]
        swaps.sort(key=lambda s: (s.block_time, s.slot))
        return swaps
