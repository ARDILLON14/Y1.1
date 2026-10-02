"""Backfill: an incomplete history is not taken as complete, and retries download only what is missing."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from copytrader.collector.wallet_collector import MAX_BACKFILL_ATTEMPTS
from copytrader.core.models import SwapEvent
from copytrader.core.types import Side, TxSource
from copytrader.db.repositories import TransactionRepo, WalletRepo
from tests.helpers import swap

WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"


class ScriptedHistory:
    """Returns ``batches`` in order, each with how many transactions it 'could not download'."""

    def __init__(self, batches: list[tuple[list[SwapEvent], int]]) -> None:
        self.batches = batches
        self.missing: dict[str, int] = {}
        self.skips: list[set[str]] = []

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
        self.skips.append(set(skip))
        swaps, missing = self.batches.pop(0)
        self.missing[wallet] = missing
        return [s for s in swaps if s.signature not in skip]


async def _backfilled(c: Any) -> bool:
    async with c.db.session() as s:
        w = await WalletRepo(s).get_by_address(WALLET)
        return w is not None and w.backfilled_at is not None


async def _stored(c: Any) -> int:
    async with c.db.session() as s:
        w = await WalletRepo(s).get_by_address(WALLET)
        assert w is not None
        return await TransactionRepo(s).count_for_wallet(w.id)


async def test_incomplete_history_is_retried_and_only_the_missing_part_downloaded(container):
    c = container
    await c.collector.add_wallet(WALLET)
    t = c.clock.now() - timedelta(days=1)
    a = swap(WALLET, MINT, Side.BUY, t, 10, 100, sig="a")
    b = swap(WALLET, MINT, Side.SELL, t + timedelta(hours=1), 10, 150, sig="b")
    history = ScriptedHistory([([a], 1), ([a, b], 0)])
    c.collector.history = history

    assert await c.collector.backfill(WALLET) == 1
    assert not await _backfilled(c)  # 1 transaction missing: the next cycle tries again
    assert await c.collector.backfill(WALLET) == 1
    assert history.skips == [set(), {"a"}]  # second pass skips what is already stored
    assert await _backfilled(c) and await _stored(c) == 2
    assert await c.collector.backfill(WALLET) == 0  # done: not downloaded again
    assert len(history.skips) == 2


async def test_history_that_never_completes_is_accepted_after_the_last_attempt(container):
    c = container
    await c.collector.add_wallet(WALLET)
    t = c.clock.now() - timedelta(days=1)
    a = swap(WALLET, MINT, Side.BUY, t, 10, 100, sig="a")
    c.collector.history = ScriptedHistory([([a], 5)] * MAX_BACKFILL_ATTEMPTS)
    for _ in range(MAX_BACKFILL_ATTEMPTS - 1):
        await c.collector.backfill(WALLET)
        assert not await _backfilled(c)
    await c.collector.backfill(WALLET)
    assert await _backfilled(c)  # stop spending RPC credits on it
