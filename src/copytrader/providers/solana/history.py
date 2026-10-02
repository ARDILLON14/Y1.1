"""Historical swaps via ``getSignaturesForAddress`` + ``getTransaction``.

Used for the initial backfill of each wallet and for catch-up after a
WebSocket disconnection (fetch everything after the last signature we saw).

A backfill is thousands of calls, so it is patient: when the RPC throttles us
(429) or its circuit breaker is open, each call waits and tries again instead
of being dropped. Whatever still could not be downloaded is counted in
``missing`` so the collector does not take a partial history as complete.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import datetime
from functools import partial
from typing import Any, TypeVar

import structlog

from copytrader.core.clock import Clock, SystemClock, from_unix
from copytrader.core.errors import CircuitOpenError, CopyTraderError, ProviderError
from copytrader.core.models import SwapEvent
from copytrader.core.types import TxSource
from copytrader.providers.interfaces import SolPriceHistory
from copytrader.providers.solana.parser import parse_swaps
from copytrader.providers.solana.rpc import SolanaRpc

log = structlog.get_logger(__name__)
T = TypeVar("T")

# Waits (s) between attempts of one call while the RPC is throttling us or its circuit
# breaker is open. ~100 s in total: longer than the breaker's default reset (30 s).
PATIENCE: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 30.0)


def _transient(exc: ProviderError) -> bool:
    return isinstance(exc, CircuitOpenError) or exc.retryable


class RpcHistorySource:
    def __init__(
        self,
        rpc: SolanaRpc,
        sol_prices: SolPriceHistory,
        *,
        quote_mints: list[str],
        concurrency: int = 4,
        clock: Clock | None = None,
        patience: Sequence[float] = PATIENCE,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.rpc = rpc
        self.sol_prices = sol_prices
        self.quote_mints = quote_mints
        self._sem = asyncio.Semaphore(concurrency)
        self._patience = tuple(patience)
        self._sleep = sleep
        # Transactions of the last fetch of each wallet that could not be downloaded.
        self.missing: dict[str, int] = {}
        self.clock = clock or SystemClock()
        # Newest signature fully scanned per wallet (swap or not). Lets periodic
        # catch-up skip non-swap activity instead of re-downloading it each time.
        self.newest_scanned: dict[str, str] = {}

    async def list_signatures(
        self, wallet: str, *, since: datetime | None, until_signature: str | None, max_signatures: int
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        before: str | None = None
        while len(collected) < max_signatures:
            limit = min(1000, max_signatures - len(collected))
            batch = await self._patiently(
                partial(self.rpc.get_signatures_for_address, wallet, before=before, until=until_signature, limit=limit)
            )
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

    async def _patiently(self, call: Callable[[], Awaitable[T]]) -> T:
        """``call()``, waiting and retrying while the error is transient (429, breaker open, timeout, 5xx)."""
        for delay in (*self._patience, None):
            try:
                return await call()
            except ProviderError as exc:
                if delay is None or not _transient(exc):
                    raise
            await self._sleep(delay)
        raise AssertionError("unreachable")

    async def fetch_swaps(
        self,
        wallet: str,
        *,
        since: datetime | None = None,
        until_signature: str | None = None,
        max_signatures: int = 1000,
        source: TxSource = TxSource.BACKFILL,
        skip: Collection[str] = (),
    ) -> list[SwapEvent]:
        """Swaps of ``wallet`` (oldest first). ``skip``: signatures already stored, not downloaded again."""
        sigs = await self.list_signatures(
            wallet, since=since, until_signature=until_signature, max_signatures=max_signatures
        )
        now = self.clock.now()
        failed: list[str] = []

        async def get(signature: str) -> dict[str, Any] | None:
            async with self._sem:  # held per attempt: waiting never blocks other downloads
                return await self.rpc.get_transaction(signature)

        async def one(item: dict[str, Any]) -> list[SwapEvent]:
            try:
                tx = await self._patiently(lambda: get(item["signature"]))
            except CopyTraderError as exc:
                failed.append(str(exc))
                return []
            if not tx:
                failed.append("transacción no disponible todavía")  # must be retried next time
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

        todo = [item for item in sigs if item["signature"] not in skip]
        results = await asyncio.gather(*(one(item) for item in todo))
        self.missing[wallet] = len(failed)
        if failed:
            log.warning("history_tx_fetch_failed", wallet=wallet, missing=len(failed), of=len(todo), error=failed[-1])
        elif sigs and len(sigs) < max_signatures:
            self.newest_scanned[wallet] = sigs[0]["signature"]
        swaps = [s for group in results for s in group]
        swaps.sort(key=lambda s: (s.block_time, s.slot))
        return swaps
