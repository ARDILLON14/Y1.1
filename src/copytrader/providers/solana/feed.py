"""Real-time Solana swap feed: WebSocket notices → transactions → parsed swaps.

Responsibilities:
* one or more streams at once (e.g. a backup provider): every transaction is
  handled once, as soon as the FIRST stream delivers it;
* in-memory dedupe of signatures (a tx can be notified several times);
* fetch the full transaction when the stream only carries the signature
  (short, growing retries: a freshly confirmed tx may not be queryable for
  ~100 ms). If another stream delivers the full transaction meanwhile, it is
  used right away instead of waiting for the RPC;
* parse swaps for every tracked wallet present in the transaction;
* catch-up after reconnects and periodic reconciliation, using the last
  signature stored per wallet — so a disconnection never silently loses trades.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any

import structlog

from copytrader.core.clock import Clock
from copytrader.core.concurrency import LRUSet
from copytrader.core.errors import CopyTraderError
from copytrader.core.types import TxSource
from copytrader.observability import metrics
from copytrader.observability.speed import SpeedStats
from copytrader.providers.interfaces import SwapHandler
from copytrader.providers.solana.history import RpcHistorySource
from copytrader.providers.solana.parser import parse_swaps
from copytrader.providers.solana.rpc import SolanaRpc
from copytrader.providers.solana.ws import HeliusTransactionStream, LogsSubscribeStream, StreamNotice

log = structlog.get_logger(__name__)

CursorLookup = Callable[[str], Awaitable[str | None]]
SolPriceFn = Callable[[], Awaitable[float | None]]
Stream = LogsSubscribeStream | HeliusTransactionStream

MAX_TX_RETRY_DELAY = 1.0  # seconds between getTransaction attempts, at most
_FIRST_SEEN_SIZE = 5_000


def tx_retry_delays(retries: int, first_delay: float, growth: float = 1.6) -> list[float]:
    """Waits between getTransaction attempts: short at first (the tx is usually there
    within a few hundred ms), then growing up to ``MAX_TX_RETRY_DELAY``."""
    return [min(MAX_TX_RETRY_DELAY, first_delay * growth**i) for i in range(max(retries - 1, 0))]


class SolanaSwapFeed:
    def __init__(
        self,
        stream: Stream | Sequence[Stream],
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
        speed: SpeedStats | None = None,
    ) -> None:
        self.streams: list[Stream] = list(stream) if isinstance(stream, Sequence) else [stream]
        if not self.streams:
            raise ValueError("at least one stream is required")
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
        self.speed = speed
        # signature -> (stream, monotonic arrival, carried the full tx) of the first delivery
        self._first_seen: OrderedDict[str, tuple[str, float, bool]] = OrderedDict()
        # signature -> future resolved when some stream delivers the full transaction
        self._tx_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # full transactions delivered by a slower stream before the fetch started
        self._late_tx: OrderedDict[str, dict[str, Any]] = OrderedDict()
        for s in self.streams:
            s._on_reconnect = self.catch_up

    @property
    def stream(self) -> Stream:
        return self.streams[0]

    def set_wallets(self, wallets: set[str]) -> None:
        self._wallets = set(wallets)
        for s in self.streams:
            s.set_wallets(self._wallets)

    async def run(self, on_swap: SwapHandler) -> None:
        self._on_swap = on_swap
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._worker(i)) for i in range(self._workers)]
        self._tasks.append(loop.create_task(self._reconcile_loop()))
        # Initial catch-up covers the gap since the previous run of the process.
        self._tasks.append(loop.create_task(self.catch_up()))
        try:
            await asyncio.gather(*(s.run(self._on_notice) for s in self.streams))
        finally:
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stopped.set()
        for s in self.streams:
            await s.stop()

    async def _on_notice(self, notice: StreamNotice) -> None:
        now = time.monotonic()
        if not self._seen.add(notice.signature):
            self._record_duplicate(notice, now)
            if notice.transaction:
                # a slower stream can still save the RPC round-trip of the one that won
                waiter = self._tx_waiters.get(notice.signature)
                if waiter is not None:
                    if not waiter.done():
                        waiter.set_result(notice.transaction)
                elif (first := self._first_seen.get(notice.signature)) is not None and not first[2]:
                    # the winner only had the signature and its fetch has not started yet
                    self._late_tx[notice.signature] = notice.transaction
                    while len(self._late_tx) > 200:
                        self._late_tx.popitem(last=False)
            return
        if len(self.streams) > 1:
            self._first_seen[notice.signature] = (notice.stream, now, notice.transaction is not None)
            while len(self._first_seen) > _FIRST_SEEN_SIZE:
                self._first_seen.popitem(last=False)
        if self.speed is not None:
            self.speed.record_notice(notice.stream or "stream", first=True)
        metrics.STREAM_FIRST.labels(stream=notice.stream or "stream").inc()
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

    def _record_duplicate(self, notice: StreamNotice, now: float) -> None:
        if self.speed is None:
            return
        self.speed.record_notice(notice.stream or "stream", first=False)
        first = self._first_seen.get(notice.signature)
        if first is not None and first[0] != notice.stream:
            self.speed.record_lead(first[0], (now - first[1]) * 1000)

    async def _get_tx(self, signature: str) -> dict[str, Any] | None:
        try:
            return await self.rpc.get_transaction(signature)
        except CopyTraderError as exc:
            log.debug("get_transaction_failed", signature=signature, error=str(exc))
            return None

    async def _fetch_tx(self, signature: str) -> dict | None:
        """The transaction from the RPC, or from another stream if it delivers it first."""
        if (late := self._late_tx.pop(signature, None)) is not None:
            return late
        waiter: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._tx_waiters[signature] = waiter
        delays = tx_retry_delays(self.tx_retries, self.tx_retry_delay)
        fetch: asyncio.Future[dict[str, Any] | None] | None = None
        try:
            for attempt in range(self.tx_retries):
                fetch = asyncio.ensure_future(self._get_tx(signature))
                racers: list[asyncio.Future[Any]] = [fetch, waiter]
                await asyncio.wait(racers, return_when=asyncio.FIRST_COMPLETED)
                if waiter.done():
                    fetch.cancel()
                    return waiter.result()
                tx = fetch.result()
                if tx:
                    return tx
                if attempt < len(delays):
                    with contextlib.suppress(TimeoutError):
                        return await asyncio.wait_for(asyncio.shield(waiter), timeout=delays[attempt])
            log.warning("transaction_not_available", signature=signature)
            return None
        finally:
            self._tx_waiters.pop(signature, None)
            if not waiter.done():
                waiter.cancel()
            if fetch is not None and not fetch.done():
                fetch.cancel()

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
                    if self.speed is not None:
                        self.speed.record_detection(notice.stream or "stream", swap.detection_latency_ms / 1000)
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
