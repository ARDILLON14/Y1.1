"""Wallet history download: waits out throttling instead of dropping transactions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from copytrader.core.errors import CircuitOpenError, ProviderError, RateLimitedError
from copytrader.providers.solana.constants import TOKEN_ACCOUNT_RENT_LAMPORTS
from copytrader.providers.solana.history import RpcHistorySource

WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
BLOCK_TIME = 1_750_000_000


def buy_tx(sig: str) -> dict[str, Any]:
    spent, fee = 1_000_000_000, 5000
    return {
        "slot": 123,
        "blockTime": BLOCK_TIME,
        "meta": {
            "err": None,
            "fee": fee,
            "preBalances": [10_000_000_000, 0, 5_000_000_000],
            "postBalances": [
                10_000_000_000 - spent - TOKEN_ACCOUNT_RENT_LAMPORTS - fee,
                TOKEN_ACCOUNT_RENT_LAMPORTS,
                5_000_000_000 + spent,
            ],
            "preTokenBalances": [],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": MINT,
                    "owner": WALLET,
                    "uiTokenAmount": {"amount": "1000000000", "decimals": 6},
                },
            ],
        },
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [
                    {"pubkey": k, "signer": i == 0, "writable": True} for i, k in enumerate([WALLET, "ata1", "pool"])
                ]
            },
        },
    }


class FakeRpc:
    """``errors[sig]``: raised in order before the transaction is returned; ``always[sig]``: raised every time."""

    def __init__(
        self,
        sigs: list[str],
        errors: dict[str, list[Exception]] | None = None,
        always: dict[str, Exception] | None = None,
        sig_errors: list[Exception] | None = None,
    ) -> None:
        self.sigs = sigs
        self.errors = {k: list(v) for k, v in (errors or {}).items()}
        self.always = always or {}
        self.sig_errors = list(sig_errors or [])
        self.tx_calls: dict[str, int] = {}

    async def get_signatures_for_address(
        self, wallet: str, *, before: str | None = None, until: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        if self.sig_errors:
            raise self.sig_errors.pop(0)
        if before is not None:
            return []
        return [{"signature": s, "blockTime": BLOCK_TIME, "err": None} for s in self.sigs]

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        self.tx_calls[signature] = self.tx_calls.get(signature, 0) + 1
        if signature in self.always:
            raise self.always[signature]
        pending = self.errors.get(signature)
        if pending:
            raise pending.pop(0)
        return buy_tx(signature)


class FakeSolPrices:
    """``series_errors``: raised in order by ``sol_series``; ``point_errors``: by ``sol_price_at``."""

    def __init__(self, series_errors: list[Exception] | None = None, point_errors: list[Exception] | None = None):
        self.series_errors = list(series_errors or [])
        self.point_errors = list(point_errors or [])
        self.series_calls = 0

    async def sol_price_at(self, ts: datetime) -> float:
        if self.point_errors:
            raise self.point_errors.pop(0)
        return 150.0

    async def sol_series(self, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        self.series_calls += 1
        if self.series_errors:
            raise self.series_errors.pop(0)
        return [(start, 150.0)]


def _source(
    rpc: FakeRpc, patience: tuple[float, ...] = (1.0, 2.0, 5.0), prices: FakeSolPrices | None = None
) -> tuple[RpcHistorySource, list[float]]:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    src = RpcHistorySource(  # type: ignore[arg-type]
        rpc, prices or FakeSolPrices(), quote_mints=[], patience=patience, sleep=sleep
    )
    return src, slept


async def test_throttling_and_open_breaker_are_waited_out():
    rpc = FakeRpc(
        ["s1", "s2", "s3"],
        errors={"s1": [RateLimitedError("429"), RateLimitedError("429")], "s2": [CircuitOpenError("solana_rpc")]},
        sig_errors=[CircuitOpenError("solana_rpc")],
    )
    src, slept = _source(rpc)
    swaps = await src.fetch_swaps(WALLET)
    assert sorted(s.signature for s in swaps) == ["s1", "s2", "s3"]
    assert src.missing[WALLET] == 0
    assert rpc.tx_calls == {"s1": 3, "s2": 2, "s3": 1}
    assert sorted(slept) == [1.0, 1.0, 1.0, 2.0]  # signatures once, s1 twice, s2 once
    assert src.newest_scanned[WALLET] == "s1"


async def test_what_could_not_be_downloaded_is_counted_not_hidden():
    rpc = FakeRpc(
        ["s1", "s2", "s3"],
        always={"s2": CircuitOpenError("solana_rpc"), "s3": ProviderError("bad request", retryable=False)},
    )
    src, _ = _source(rpc, patience=(0.0, 0.0))
    swaps = await src.fetch_swaps(WALLET)
    assert [s.signature for s in swaps] == ["s1"]
    assert src.missing[WALLET] == 2
    assert rpc.tx_calls["s2"] == 3  # patience exhausted
    assert rpc.tx_calls["s3"] == 1  # not transient: no retries
    assert WALLET not in src.newest_scanned  # incomplete: catch-up must not skip past it


async def test_already_stored_signatures_are_not_downloaded_again():
    rpc = FakeRpc(["s1", "s2", "s3"])
    src, _ = _source(rpc)
    swaps = await src.fetch_swaps(WALLET, skip={"s1", "s3"})
    assert [s.signature for s in swaps] == ["s2"]
    assert rpc.tx_calls == {"s2": 1}
    assert src.missing[WALLET] == 0


async def test_signature_listing_gives_up_after_its_patience():
    rpc = FakeRpc(["s1"], sig_errors=[CircuitOpenError("solana_rpc")] * 3)
    src, _ = _source(rpc, patience=(0.0, 0.0))
    with pytest.raises(CircuitOpenError):
        await src.fetch_swaps(WALLET)


async def test_sol_price_failure_never_discards_the_downloaded_transactions():
    """A SOL price lookup that fails for one transaction used to abort the whole wallet:
    every transaction already downloaded (and paid for in RPC credits) was thrown away."""
    rpc = FakeRpc(["s1", "s2", "s3"])
    prices = FakeSolPrices(point_errors=[ProviderError("sol_price_history: timeout")] * 3)
    src, _ = _source(rpc, patience=(0.0, 0.0), prices=prices)
    swaps = await src.fetch_swaps(WALLET)
    assert len(swaps) == 2 and src.missing[WALLET] == 1  # one could not be valued: retried next cycle
    assert rpc.tx_calls == {"s1": 1, "s2": 1, "s3": 1}


async def test_unreachable_sol_prices_stop_before_spending_credits():
    rpc = FakeRpc(["s1", "s2"])
    prices = FakeSolPrices(series_errors=[ProviderError("sol_price_history: timeout")] * 3)
    src, _ = _source(rpc, patience=(0.0, 0.0), prices=prices)
    with pytest.raises(ProviderError):
        await src.fetch_swaps(WALLET)
    assert rpc.tx_calls == {}  # no getTransaction was paid for
    assert prices.series_calls == 3  # waited out its patience first


async def test_sol_prices_are_loaded_once_for_the_whole_period():
    rpc = FakeRpc(["s1", "s2", "s3"])
    prices = FakeSolPrices(series_errors=[ProviderError("sol_price_history: timeout")])
    src, slept = _source(rpc, prices=prices)
    swaps = await src.fetch_swaps(WALLET)
    assert len(swaps) == 3 and src.missing[WALLET] == 0
    assert prices.series_calls == 2 and slept == [1.0]
