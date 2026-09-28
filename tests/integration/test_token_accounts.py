"""Closing empty token accounts (rent recovery) is safe and accepted by the signer policy."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from sqlalchemy import select

from copytrader.core.types import TradeMode
from copytrader.db.models import EventLog, Position
from copytrader.execution.live import LiveExecutor
from copytrader.execution.token_accounts import TokenAccountJanitor, build_close_accounts_tx
from copytrader.providers.solana.constants import SOL_MINT, TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from copytrader.providers.solana.rpc import TokenAccount
from copytrader.security.signer import LocalSigner
from copytrader.security.signer_policy import SignerLimits, SignerPolicy, SignIntent
from tests.integration.conftest import seeded_container

RENT = 2_039_280
KP = Keypair()
OWNER = str(KP.pubkey())


def _acc(mint: str, amount: int = 0, program: str = TOKEN_PROGRAM) -> TokenAccount:
    return TokenAccount(address=str(Pubkey.new_unique()), mint=mint, amount_raw=amount, program=program, lamports=RENT)


def _mint() -> str:
    return str(Pubkey.new_unique())


class FakeChain:
    def __init__(self, accounts: list[TokenAccount]) -> None:
        self.accounts = accounts
        self.sent: list[bytes] = []
        self.status: dict[str, Any] | None = {"confirmationStatus": "confirmed", "err": None}

    async def get_token_accounts(self, owner: str) -> list[TokenAccount]:
        assert owner == OWNER
        return list(self.accounts)

    async def get_latest_blockhash(self) -> tuple[str, int]:
        return str(Hash.default()), 1_000

    async def send_raw_transaction(self, tx_bytes: bytes, *, skip_preflight: bool = True) -> str:
        self.sent.append(tx_bytes)
        return "sig"

    async def get_signature_statuses(self, signatures: list[str]) -> list[dict[str, Any] | None]:
        return [self.status]

    async def get_block_height(self) -> int:
        return 10


def _closed_accounts(tx_bytes: bytes) -> list[str]:
    tx = VersionedTransaction.from_bytes(tx_bytes)
    keys = [str(k) for k in tx.message.account_keys]
    return [keys[ix.accounts[0]] for ix in tx.message.instructions if bytes(ix.data) == b"\x09"]


def test_close_transaction_passes_the_signer_policy():
    accounts = [_acc(_mint()), _acc(_mint(), program=TOKEN_2022_PROGRAM)]
    tx_bytes = build_close_accounts_tx(OWNER, accounts, str(Hash.default()))
    policy = SignerPolicy(owner=OWNER, limits=SignerLimits())
    intent = SignIntent("close-1", "maintenance", "", "", 0, 0.0)
    assert policy.inspect(VersionedTransaction.from_bytes(tx_bytes), intent) == []
    assert _closed_accounts(tx_bytes) == [a.address for a in accounts]


def test_policy_rejects_a_close_that_refunds_someone_else():
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0

    thief = Pubkey.new_unique()
    ix = Instruction(
        Pubkey.from_string(TOKEN_PROGRAM),
        b"\x09",
        [
            AccountMeta(Pubkey.new_unique(), False, True),
            AccountMeta(thief, False, True),
            AccountMeta(KP.pubkey(), True, False),
        ],
    )
    message = MessageV0.try_compile(KP.pubkey(), [ix], [], Hash.default())
    tx = VersionedTransaction.populate(message, [Signature.default()])
    violations = SignerPolicy(owner=OWNER).inspect(tx, SignIntent("x", "maintenance", "", "", 0, 0.0))
    assert any("CloseAccount" in v for v in violations)


@pytest.fixture
async def janitor_env(tmp_path, template_db):
    c = await seeded_container(tmp_path, template_db, {"execution": {"wallet_public_key": OWNER}})
    chain = FakeChain([])
    live = LiveExecutor(
        quotes=c.providers.quotes,
        builder=None,  # type: ignore[arg-type]  (never builds swaps here)
        chain=chain,  # type: ignore[arg-type]
        signer=LocalSigner(KP, SignerPolicy(owner=OWNER)),
        tokens=c.tokens,
        clock=c.clock,
        config=c.get_cfg,
    )
    janitor = TokenAccountJanitor(db=c.db, chain=chain, live=live, mode=c.mode, config=c.get_cfg, locks=c.token_locks)
    yield c, chain, janitor
    await c.aclose()


async def test_nothing_is_sent_while_live_trading_is_not_armed(janitor_env):
    _, chain, janitor = janitor_env
    chain.accounts = [_acc(_mint())]
    report = await janitor.run_once()
    assert report.outcome == "not_armed" and chain.sent == []


async def test_closes_only_empty_idle_non_quote_accounts(janitor_env):
    c, chain, janitor = janitor_env
    c.mode.live_block_reason = lambda: None  # type: ignore[method-assign]  (armed)
    busy_mint = _mint()
    empty, with_balance, busy, wsol = _acc(_mint()), _acc(_mint(), amount=5), _acc(busy_mint), _acc(SOL_MINT)
    chain.accounts = [empty, with_balance, busy, wsol]
    now = c.clock.now()
    async with c.db.session() as s:  # an open LIVE position still uses busy_mint's account
        s.add(
            Position(
                mode=TradeMode.LIVE.value,
                token_mint=busy_mint,
                decimals=6,
                exit_mode="protected",
                status="open",
                qty_raw=1,
                initial_qty_raw=1,
                cost_usd=1,
                initial_cost_usd=1,
                entry_price_usd=1,
                peak_price_usd=1,
                opened_at=now - timedelta(minutes=1),
            )
        )
    report = await janitor.run_once()
    assert report.outcome == "closed"
    assert report.closed == 1 and report.skipped_busy == 1
    assert report.rent_recovered_sol == pytest.approx(RENT / 1e9)
    assert {_closed_accounts(tx)[0] for tx in chain.sent} == {empty.address}
    assert VersionedTransaction.from_bytes(chain.sent[0]).signatures[0] != Signature.default()  # signed
    async with c.db.session() as s:
        event = (await s.execute(select(EventLog).where(EventLog.event == "token_accounts_closed"))).scalar_one()
    assert event.data["accounts"] == 1 and event.data["mints"] == [empty.mint]


async def test_failed_close_backs_off_instead_of_retrying_every_run(janitor_env):
    c, chain, janitor = janitor_env
    c.mode.live_block_reason = lambda: None  # type: ignore[method-assign]
    chain.accounts = [_acc(_mint(), program=TOKEN_2022_PROGRAM)]
    chain.status = {"err": {"InstructionError": [2, {"Custom": 37}]}}  # e.g. withheld transfer fees
    first = await janitor.run_once()
    assert first.outcome == "failed"
    sent = len(chain.sent)
    second = await janitor.run_once()
    assert second.candidates == 0 and len(chain.sent) == sent  # backing off


async def test_disabled_by_configuration(tmp_path, template_db):
    c = await seeded_container(
        tmp_path,
        template_db,
        {"execution": {"wallet_public_key": OWNER, "close_empty_token_accounts": False}},
    )
    try:
        chain = FakeChain([_acc(_mint())])
        live = LiveExecutor(
            quotes=c.providers.quotes,
            builder=None,  # type: ignore[arg-type]
            chain=chain,  # type: ignore[arg-type]
            signer=LocalSigner(KP, SignerPolicy(owner=OWNER)),
            tokens=c.tokens,
            clock=c.clock,
            config=c.get_cfg,
        )
        c.mode.live_block_reason = lambda: None  # type: ignore[method-assign]
        janitor = TokenAccountJanitor(
            db=c.db, chain=chain, live=live, mode=c.mode, config=c.get_cfg, locks=c.token_locks
        )
        assert (await janitor.run_once()).outcome == "disabled" and chain.sent == []
    finally:
        await c.aclose()
