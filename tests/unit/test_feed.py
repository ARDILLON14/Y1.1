"""Real-time feed with several streams: first delivery wins, a slower stream's full tx saves the RPC."""

from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime

import pytest

from copytrader.core.clock import SystemClock
from copytrader.observability.speed import SpeedStats, percentile
from copytrader.providers.solana.feed import MAX_TX_RETRY_DELAY, SolanaSwapFeed, tx_retry_delays
from copytrader.providers.solana.ws import StreamNotice


class FakeStream:
    def __init__(self, name: str) -> None:
        self.name = name
        self.wallets: set[str] = set()
        self._on_reconnect = None

    def set_wallets(self, wallets: set[str]) -> None:
        self.wallets = set(wallets)

    async def run(self, sink) -> None:  # pragma: no cover - not used
        await asyncio.Event().wait()

    async def stop(self) -> None:
        pass


class SlowRpc:
    """getTransaction that is not ready for a while (as right after confirmation)."""

    def __init__(self, ready_after: float, tx: dict | None = None) -> None:
        self.ready_at = asyncio.get_running_loop().time() + ready_after
        self.tx = tx
        self.calls = 0

    async def get_transaction(self, signature: str):
        self.calls += 1
        await asyncio.sleep(0.01)
        return self.tx if asyncio.get_running_loop().time() >= self.ready_at else None


def _feed(rpc, streams, speed=None, retries=12, delay=0.1) -> SolanaSwapFeed:
    async def no_price():
        return 150.0

    async def no_cursor(_):
        return None

    return SolanaSwapFeed(
        streams,
        rpc,
        history=None,  # type: ignore[arg-type]
        sol_price=no_price,
        cursor_lookup=no_cursor,
        quote_mints=[],
        clock=SystemClock(),
        tx_retries=retries,
        tx_retry_delay=delay,
        speed=speed,
    )


def _notice(sig: str, stream: str, tx: dict | None = None) -> StreamNotice:
    return StreamNotice(signature=sig, slot=1, received_at=datetime.now(UTC), transaction=tx, stream=stream)


def test_retry_delays_start_short_and_are_capped():
    delays = tx_retry_delays(12, 0.1)
    assert len(delays) == 11
    assert delays[0] == pytest.approx(0.1)
    assert delays[1] == pytest.approx(0.16)
    assert all(b >= a for a, b in itertools.pairwise(delays))
    assert max(delays) == MAX_TX_RETRY_DELAY
    # the first second already has several attempts (the old linear 250 ms schedule had 2)
    assert sum(1 for i in range(len(delays)) if sum(delays[: i + 1]) <= 1.0) >= 4
    assert tx_retry_delays(1, 0.1) == []


async def test_first_stream_wins_and_duplicates_are_measured():
    speed = SpeedStats()
    feed = _feed(SlowRpc(0), [FakeStream("helius"), FakeStream("backup_logs")], speed)
    await feed._on_notice(_notice("S1", "helius", {"x": 1}))
    await asyncio.sleep(0.02)
    await feed._on_notice(_notice("S1", "backup_logs"))
    await feed._on_notice(_notice("S2", "backup_logs"))
    assert feed._queue.qsize() == 2  # S1 once, S2 once
    snap = {s["stream"]: s for s in speed.snapshot()["streams"]}
    assert snap["helius"]["first"] == 1 and snap["backup_logs"]["first"] == 1
    assert snap["backup_logs"]["notices"] == 2
    assert snap["helius"]["lead_ms"]["n"] == 1 and snap["helius"]["lead_ms"]["p50"] >= 15


async def test_full_tx_from_slower_stream_beats_the_rpc():
    rpc = SlowRpc(ready_after=5.0, tx={"from": "rpc"})
    feed = _feed(rpc, [FakeStream("logs"), FakeStream("backup_helius")])
    await feed._on_notice(_notice("S1", "logs"))
    fetch = asyncio.create_task(feed._fetch_tx("S1"))
    await asyncio.sleep(0.05)
    assert not fetch.done()
    await feed._on_notice(_notice("S1", "backup_helius", {"from": "stream"}))
    tx = await asyncio.wait_for(fetch, 0.5)
    assert tx == {"from": "stream"}
    assert not feed._tx_waiters


async def test_full_tx_delivered_before_the_fetch_starts_is_kept():
    rpc = SlowRpc(ready_after=5.0, tx={"from": "rpc"})
    feed = _feed(rpc, [FakeStream("logs"), FakeStream("backup_helius")])
    await feed._on_notice(_notice("S1", "logs"))
    await feed._on_notice(_notice("S1", "backup_helius", {"from": "stream"}))
    assert await feed._fetch_tx("S1") == {"from": "stream"}
    assert rpc.calls == 0


async def test_duplicate_full_tx_is_not_kept_when_the_winner_had_it():
    feed = _feed(SlowRpc(0), [FakeStream("helius"), FakeStream("backup_helius")])
    await feed._on_notice(_notice("S1", "helius", {"x": 1}))
    await feed._on_notice(_notice("S1", "backup_helius", {"x": 1}))
    assert not feed._late_tx


async def test_fetch_gives_up_after_the_retries():
    rpc = SlowRpc(ready_after=60.0)
    feed = _feed(rpc, [FakeStream("logs")], retries=3, delay=0.02)
    assert await feed._fetch_tx("S1") is None
    assert rpc.calls == 3


async def test_single_stream_keeps_working_and_set_wallets_reaches_every_stream():
    a, b = FakeStream("a"), FakeStream("b")
    feed = _feed(SlowRpc(0, {"ok": 1}), [a, b])
    feed.set_wallets({"W1", "W2"})
    assert a.wallets == b.wallets == {"W1", "W2"}
    assert feed.stream is a
    single = _feed(SlowRpc(0, {"ok": 1}), FakeStream("only"))
    assert await single._fetch_tx("S") == {"ok": 1}


def test_percentile_nearest_rank():
    assert percentile([], 0.5) is None
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 10.0], 0.9) == 10.0
