"""Real-time Solana swap feed: WebSocket notices → transactions → parsed swaps.

Responsibilities:
* in-memory dedupe of signatures (a tx can be notified several times);
* fetch the full transaction when the stream only carries the signature
  (short retries: a freshly confirmed tx may not be queryable for ~100 ms);
* parse swaps for every tracked wallet present in the transaction;
* catch-up after reconnects and periodic reconciliation, using the last
  signature stored per wallet — so a disconnection never silently loses trades.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog

from copytrader.core.clock import Clock
from copytrader.core.concurrency import LRUSet
from copytrader.core.errors import CopyTraderError
from copytrader.core.types import TxSource
from copytrader.observability import metrics
from copytrader.providers.interfaces import SwapHandler
from copytrader.providers.solana.history import RpcHistorySource
from copytrader.providers.solana.parser import parse_swaps
from copytrader.providers.solana.rpc import SolanaRpc
from copytrader.providers.solana.ws import HeliusTransactionStream, LogsSubscribeStream, StreamNotice

log = structlog.get_logger(__name__)

CursorLookup = Callable[[str], Awaitable[str | None]]
SolPriceFn = Callable[[], Awaitable[float | None]]


class SolanaSwapFeed:
    def __init__(
        self,
        stream: LogsSubscribeStream | HeliusTransactionStream,
        rpc: SolanaRpc,
        history: RpcHistorySource,
        *,
        sol_price: SolPriceFn,
        cursor_lookup: CursorLookup,
        quote_mints: list[str],
        clock: Clock,
        tx_retries: int = 8,
        tx_retry_delay: float = 0.25,
        reconcile_interval: float = 300.0,
        workers: int = 8,
        dedupe_size: int = 50_000,
    ) -> None:
        self.stream = stream
        self.rpc = rpc
        self.history = history
        self._sol_price = sol_price
        self._cursor = cursor_lookup
        self.quote_mints = quote_mints
        self.clock = clock
        self.tx_retries = tx_retries
        self.tx_retry_delay = tx_retry_delay
        self.reconcile_interval = reconcile_interval
        self._wallets: set[str] = set()
        self._seen = LRUSet(dedupe_size)
        self._queue: asyncio.Queue[StreamNotice] = asyncio.Queue(maxsize=10_000)
        self._workers = workers
        self._on_swap: SwapHandler | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stopped = asyncio.Event()
        self._catchup_lock = asyncio.Lock()
        stream._on_reconnect = self.catch_up

    def set_wallets(self, wallets: set[str]) -> None:
        self._wallets = set(wallets)
        self.stream.set_wallets(self._wallets)

    async def run(self, on_swap: SwapHandler) -> None:
        self._on_swap = on_swap
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._worker(i)) for i in range(self._workers)]
        self._tasks.append(loop.create_task(self._reconcile_loop()))
        # Initial catch-up covers the gap since the previous run of the process.
        self._tasks.append(loop.create_task(self.catch_up()))
        try:
            await self.stream.run(self._on_notice)
        finally:
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stopped.set()
        await self.stream.stop()

    async def _on_notice(self, notice: StreamNotice) -> None:
        if not self._seen.add(notice.signature):
            return
        try:
            self._queue.put_nowait(notice)
        except asyncio.QueueFull:
            log.error("feed_queue_full_triggering_catchup")
            asyncio.get_running_loop().create_task(self.catch_up())
        metrics.QUEUE_DEPTH.set(self._queue.qsize())

    async def _worker(self, idx: int) -> None:
        while True:
            notice = await self._queue.get()
            try:
                await self._handle(notice)
            except asyncio.CancelledError:
                raise
            except Exception:
                metrics.ERRORS.labels(component="feed").inc()
                log.exception("feed_notice_failed", signature=notice.signature)
            finally:
                self._queue.task_done()

    async def _fetch_tx(self, signature: str) -> dict | None:
        for attempt in range(self.tx_retries):
            try:
                tx = await self.rpc.get_transaction(signature)
            except CopyTraderError as exc:
                log.debug("get_transaction_failed", signature=signature, error=str(exc))
                tx = None
            if tx:
                return tx
            await asyncio.sleep(self.tx_retry_delay * (1 + attempt))
        log.warning("transaction_not_available", signature=signature)
        return None

    async def _handle(self, notice: StreamNotice) -> None:
        tx = notice.transaction or await self._fetch_tx(notice.signature)
        if not tx or self._on_swap is None:
            return
        sol_price = await self._sol_price()
        wallets = [notice.wallet] if notice.wallet in self._wallets else []
        wallets += [w for w in self._wallets_in(tx) if w != notice.wallet]
        for wallet in wallets:
            try:
                swaps = parse_swaps(
                    tx,
                    wallet,
                    sol_price_usd=sol_price,
                    quote_mints=self.quote_mints,
                    source=TxSource.STREAM,
                    detected_at=notice.received_at,
                    fallback_time=notice.received_at,
                )
            except CopyTraderError as exc:
                log.warning("parse_failed", signature=notice.signature, error=str(exc))
                continue
            for swap in swaps:
                metrics.SWAPS_DETECTED.labels(source="stream").inc()
                if swap.detection_latency_ms is not None:
                    metrics.DETECTION_LATENCY.observe(max(0.0, swap.detection_latency_ms / 1000))
                await self._on_swap(swap)

    def _wallets_in(self, tx: dict) -> list[str]:
        from copytrader.providers.solana.parser import normalize_transaction

        try:
            keys = set(normalize_transaction(tx).account_keys)
        except CopyTraderError:
            return []
        return [w for w in self._wallets if w in keys]

    async def catch_up(self) -> None:
        """Fetch swaps missed while disconnected (bounded per wallet)."""
        if self._on_swap is None:
            return
        async with self._catchup_lock:
            since = self.clock.now() - timedelta(hours=6)
            for wallet in sorted(self._wallets):
                if self._stopped.is_set():
                    return
                try:
                    cursor = self.history.newest_scanned.get(wallet) or await self._cursor(wallet)
                    swaps = await self.history.fetch_swaps(
                        wallet,
                        since=None if cursor else since,
                        until_signature=cursor,
                        max_signatures=200,
                        source=TxSource.CATCHUP,
                    )
                except CopyTraderError as exc:
                    log.warning("catchup_failed", wallet=wallet, error=str(exc))
                    continue
                for swap in swaps:
                    if not self._seen.add(swap.signature + ":" + wallet):
                        continue
                    metrics.SWAPS_DETECTED.labels(source="catchup").inc()
                    await self._on_swap(swap)

    async def _reconcile_loop(self) -> None:
        while not self._stopped.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self.reconcile_interval)
            if not self._stopped.is_set():
                await self.catch_up()
