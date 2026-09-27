"""Live execution safety properties with fake chain / builder / signer.

* the signature is persisted (order SIGNED) BEFORE the first send;
* a confirmation timeout leaves the order SUBMITTED (never "failed" while it may land);
* recovery resolves it later and the fill is applied exactly once;
* an expired blockhash marks the order EXPIRED and never re-sends.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from sqlalchemy import func, select

from copytrader.core import ids
from copytrader.core.models import OrderRequest
from copytrader.core.types import OrderPurpose, OrderStatus, PositionStatus, Side, TradeMode
from copytrader.db.models import Execution, Order, Position
from copytrader.execution.live import LiveExecutor
from copytrader.providers.interfaces import BuiltTransaction
from copytrader.providers.solana.constants import SOL_MINT, TOKEN_ACCOUNT_RENT_LAMPORTS
from copytrader.security.signer import SignedTransaction
from tests.integration.conftest import good_token, seeded_container

WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


class FakeChain:
    def __init__(self, c: Any, mint: str) -> None:
        self.c = c
        self.mint = mint
        self.sends: list[str] = []
        self.status: str = "pending"  # pending | confirmed | failed
        self.height = itertools.count(1000)
        self.height_jump = 0
        self.orders_seen_signed: list[bool] = []
        self.last_in = 0
        self.last_out = 0

    async def send_raw_transaction(self, tx_bytes: bytes, *, skip_preflight: bool = True) -> str:
        sig = tx_bytes.decode().split("|")[1]
        async with self.c.db.session() as s:
            order = (await s.execute(select(Order).where(Order.tx_signature == sig))).scalar_one_or_none()
        self.orders_seen_signed.append(order is not None and order.status in ("signed", "submitted"))
        self.sends.append(sig)
        return sig

    async def get_signature_statuses(self, sigs: list[str]) -> list[dict | None]:
        if self.status == "confirmed":
            return [{"err": None, "confirmationStatus": "confirmed"}]
        if self.status == "failed":
            return [{"err": {"InstructionError": [3, {"Custom": 6001}]}, "confirmationStatus": "confirmed"}]
        return [None]

    async def get_block_height(self) -> int:
        return next(self.height) + self.height_jump

    async def get_transaction(self, signature: str, *, commitment: str | None = None) -> dict:
        fee = 5000
        spent = self.last_in
        return {
            "slot": 1,
            "blockTime": int(self.c.clock.now().timestamp()),
            "meta": {
                "err": None,
                "fee": fee,
                "preBalances": [10_000_000_000, 0],
                "postBalances": [
                    10_000_000_000 - spent - fee - TOKEN_ACCOUNT_RENT_LAMPORTS,
                    TOKEN_ACCOUNT_RENT_LAMPORTS,
                ],
                "preTokenBalances": [],
                "postTokenBalances": [
                    {
                        "accountIndex": 1,
                        "mint": self.mint,
                        "owner": WALLET,
                        "uiTokenAmount": {"amount": str(self.last_out), "decimals": 6},
                    }
                ],
            },
            "transaction": {
                "signatures": [signature],
                "message": {"accountKeys": [{"pubkey": WALLET}, {"pubkey": "ata"}]},
            },
        }

    async def get_balance(self, address: str) -> int:
        return 100_000_000_000

    async def get_token_balances(self, owner: str) -> dict[str, int]:
        return {self.mint: 10**18}

    async def get_health(self) -> bool:
        return True


class FakeBuilder:
    def __init__(self, chain: FakeChain) -> None:
        self.chain = chain

    async def build_swap(self, quote: Any, user: str, **_: Any) -> BuiltTransaction:
        self.chain.last_in, self.chain.last_out = quote.in_amount_raw, quote.out_amount_raw
        return BuiltTransaction(tx_bytes=b"unsigned", last_valid_block_height=1100)


class FakeSigner:
    async def public_key(self) -> str:
        return WALLET

    async def sign(self, tx_bytes: bytes, intent: Any) -> SignedTransaction:
        sig = f"sig-{intent.client_order_id}"
        return SignedTransaction(tx_bytes=f"signed|{sig}".encode(), signature=sig)

    async def healthy(self) -> bool:
        return True


@pytest.fixture
async def live(tmp_path, template_db):
    c = await seeded_container(
        tmp_path,
        template_db,
        {"execution": {"wallet_public_key": WALLET, "confirm_timeout_seconds": 1.0, "rebroadcast_interval_ms": 200}},
    )
    mint = good_token(c)
    chain = FakeChain(c, mint)
    executor = LiveExecutor(
        quotes=c.providers.quotes,
        builder=FakeBuilder(chain),
        chain=chain,
        signer=FakeSigner(),
        tokens=c.tokens,
        clock=c.clock,
        config=c.get_cfg,
    )
    c.execution.executors[TradeMode.LIVE] = executor
    c.recovery.live = executor
    c.mode.live_block_reason = lambda: None  # type: ignore[method-assign]  (arm without real preflight)
    yield c, chain, mint
    await c.aclose()


def _entry(c, mint: str, key: str) -> OrderRequest:
    sol = c.providers.simulated_market.sol_price()
    return OrderRequest(
        client_order_id=ids.entry_order_id(key),
        purpose=OrderPurpose.ENTRY,
        side=Side.BUY,
        mode=TradeMode.LIVE,
        token_mint=mint,
        token_decimals=6,
        input_mint=SOL_MINT,
        output_mint=mint,
        amount_in_raw=int(20 / sol * 1e9),
        slippage_bps=150,
        notional_usd=20.0,
    )


async def _count(c, model) -> int:
    async with c.db.session() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_confirmed_live_entry_persists_signature_before_send(live):
    c, chain, mint = live
    chain.status = "confirmed"
    result = await c.execution.execute(_entry(c, mint, "k1"), context={"decimals": 6, "exit_mode": "protected"})
    assert result.success, result.error
    assert chain.orders_seen_signed and all(chain.orders_seen_signed)  # SIGNED was in DB before every send
    assert result.token_qty == pytest.approx(chain.last_out / 1e6)
    async with c.db.session() as s:
        order = (await s.execute(select(Order))).scalar_one()
        pos = (await s.execute(select(Position))).scalar_one()
    assert order.status == OrderStatus.CONFIRMED.value and order.tx_signature == "sig-" + order.client_order_id
    assert pos.mode == "live" and pos.status == PositionStatus.OPEN.value
    # Calling execute again with the same order id never sends again
    sends = len(chain.sends)
    again = await c.execution.execute(_entry(c, mint, "k1"), context={})
    assert again.success and len(chain.sends) == sends
    assert await _count(c, Execution) == 1


async def test_timeout_keeps_order_pending_then_recovery_applies_fill_once(live):
    c, chain, mint = live
    chain.status = "pending"
    result = await c.execution.execute(_entry(c, mint, "k2"), context={"decimals": 6})
    assert not result.success and result.error == "pending"
    async with c.db.session() as s:
        order = (await s.execute(select(Order))).scalar_one()
    assert order.status == OrderStatus.SUBMITTED.value  # NOT failed: it could still land
    assert await _count(c, Position) == 0

    chain.status = "confirmed"
    await c.recovery.resolve_pending()
    await c.recovery.resolve_pending()  # idempotent
    assert await _count(c, Execution) == 1
    assert await _count(c, Position) == 1
    async with c.db.session() as s:
        order = (await s.execute(select(Order))).scalar_one()
    assert order.status == OrderStatus.CONFIRMED.value


async def test_expired_blockhash_marks_expired_and_never_resends(live):
    c, chain, mint = live
    chain.status = "pending"
    chain.height_jump = 10_000  # current height far beyond lastValidBlockHeight
    result = await c.execution.execute(_entry(c, mint, "k3"), context={"decimals": 6})
    assert not result.success and result.error == "expired"
    async with c.db.session() as s:
        order = (await s.execute(select(Order))).scalar_one()
    assert order.status == OrderStatus.EXPIRED.value
    sends = len(chain.sends)
    await c.recovery.resolve_pending()
    assert len(chain.sends) == sends
    assert await _count(c, Position) == 0


async def test_onchain_failure_is_final(live):
    c, chain, mint = live
    chain.status = "failed"
    result = await c.execution.execute(_entry(c, mint, "k4"), context={"decimals": 6})
    assert not result.success and "fallida on-chain" in result.error
    async with c.db.session() as s:
        order = (await s.execute(select(Order))).scalar_one()
    assert order.status == OrderStatus.FAILED.value


async def test_restart_resolves_signed_live_order(live):
    c, chain, mint = live
    # Simulate a crash right after persisting the signature (never sent).
    async with c.db.session() as s:
        s.add(
            Order(
                client_order_id="ent-crash",
                mode="live",
                purpose="entry",
                side="buy",
                token_mint=mint,
                input_mint=SOL_MINT,
                output_mint=mint,
                amount_in_raw=10**8,
                slippage_bps=150,
                status="signed",
                tx_signature="sig-crash",
                last_valid_block_height=5,
                notional_usd=15.0,
                context={"decimals": 6},
            )
        )
    chain.height_jump = 10_000
    counts = await c.recovery.on_startup()
    assert counts["resolved"] == 1
    async with c.db.session() as s:
        order = (await s.execute(select(Order).where(Order.client_order_id == "ent-crash"))).scalar_one()
    assert order.status == OrderStatus.EXPIRED.value
    assert chain.sends == []  # recovery never re-sends


async def test_guard_blocks_oversized_live_entry(live):
    c, chain, mint = live
    req = _entry(c, mint, "big")
    req.notional_usd = 10_000.0
    result = await c.execution.execute(req, context={})
    assert not result.success and "guardia" in result.error
    assert chain.sends == []
    assert await _count(c, Order) == 0


async def test_guard_blocks_live_when_disarmed(live):
    c, chain, mint = live
    del c.mode.live_block_reason  # back to the real gates (not armed)
    result = await c.execution.execute(_entry(c, mint, "disarmed"), context={})
    assert not result.success and "no permitido" in result.error
    assert chain.sends == []
