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
    async def sol_price_at(self, ts: datetime) -> float:
        return 150.0


def _source(rpc: FakeRpc, patience: tuple[float, ...] = (1.0, 2.0, 5.0)) -> tuple[RpcHistorySource, list[float]]:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    src = RpcHistorySource(rpc, FakeSolPrices(), quote_mints=[], patience=patience, sleep=sleep)  # type: ignore[arg-type]
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
