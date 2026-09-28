"""Closes the bot wallet's empty token accounts to recover their rent.

Buying a token creates a token account that locks ~0.00204 SOL of rent.
Jupiter does not close it after we sell, so without this service every token
ever traded would keep that SOL locked: on small positions that is a cost of
the same order as the priority fees.

Safety:
* only accounts with a zero balance are closed (the SPL program refuses otherwise);
* never while a LIVE position or an in-flight LIVE order exists for the mint,
  and the mints being closed are held with the same per-token locks as the
  copy pipeline and the position manager, so an entry can never race with the
  close of its account;
* quote-asset accounts (WSOL, USDC, USDT) are never touched;
* the transaction only contains compute-budget and ``CloseAccount``
  instructions refunding to the bot wallet, which the signer policy verifies
  independently;
* it only runs while live trading is allowed (armed), at a low rate, closing
  several accounts per transaction, so it never competes with trading for the
  signer's rate limit;
* failed accounts back off (e.g. Token-2022 accounts holding withheld fees).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from copytrader.config.models import AppConfig
from copytrader.core.concurrency import KeyedLocks
from copytrader.core.errors import CopyTraderError, ProviderError, SecurityError
from copytrader.core.types import TradeMode
from copytrader.db.base import Database
from copytrader.db.repositories import EventLogRepo, OrderRepo, PositionRepo
from copytrader.execution.live import LiveExecutor
from copytrader.execution.mode import ModeController
from copytrader.observability import metrics
from copytrader.providers.solana.rpc import TokenAccount
from copytrader.security.signer_policy import SignIntent

log = structlog.get_logger(__name__)

CLOSE_ACCOUNT_IX = 9
MAX_ACCOUNTS_PER_TX = 8
CU_PER_CLOSE = 6_000
CU_BASE = 1_000
CU_PRICE_MICRO_LAMPORTS = 1_000  # closing is not urgent: a negligible priority fee
FAILURE_BACKOFF_SECONDS = 3_600.0
MAX_FAILURE_BACKOFF_SECONDS = 86_400.0


class TokenAccountsChain(Protocol):
    async def get_token_accounts(self, owner: str) -> list[TokenAccount]: ...

    async def get_latest_blockhash(self) -> tuple[str, int]: ...

    async def send_raw_transaction(self, tx_bytes: bytes, *, skip_preflight: bool = True) -> str: ...


@dataclass
class CloseReport:
    candidates: int = 0
    closed: int = 0
    rent_recovered_sol: float = 0.0
    skipped_busy: int = 0
    signature: str | None = None
    outcome: str = "idle"  # idle | disabled | not_armed | closed | failed | pending
    mints: list[str] = field(default_factory=list)


def build_close_accounts_tx(owner: str, accounts: Sequence[TokenAccount], blockhash: str) -> bytes:
    """Unsigned v0 transaction closing ``accounts`` and refunding their rent to ``owner``."""
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction

    payer = Pubkey.from_string(owner)
    instructions = [
        set_compute_unit_limit(CU_BASE + CU_PER_CLOSE * len(accounts)),
        set_compute_unit_price(CU_PRICE_MICRO_LAMPORTS),
    ]
    for acc in accounts:
        instructions.append(
            Instruction(
                Pubkey.from_string(acc.program),
                bytes([CLOSE_ACCOUNT_IX]),
                [
                    AccountMeta(Pubkey.from_string(acc.address), is_signer=False, is_writable=True),
                    AccountMeta(payer, is_signer=False, is_writable=True),  # rent destination
                    AccountMeta(payer, is_signer=True, is_writable=False),  # account owner
                ],
            )
        )
    message = MessageV0.try_compile(payer, instructions, [], Hash.from_string(blockhash))
    return bytes(VersionedTransaction.populate(message, [Signature.default()]))


class TokenAccountJanitor:
    def __init__(
        self,
        *,
        db: Database,
        chain: TokenAccountsChain,
        live: LiveExecutor,
        mode: ModeController,
        config: Callable[[], AppConfig],
        locks: KeyedLocks,
    ) -> None:
        self.db = db
        self.chain = chain
        self.live = live
        self.mode = mode
        self._config = config
        self.locks = locks
        self._backoff: dict[str, tuple[float, int]] = {}  # account -> (retry at monotonic, failures)
        self._stopped = asyncio.Event()

    async def _busy_mints(self) -> set[str]:
        async with self.db.session() as s:
            positions = await PositionRepo(s).open_positions(TradeMode.LIVE)
            orders = await OrderRepo(s).in_flight(TradeMode.LIVE)
        return {p.token_mint for p in positions} | {o.token_mint for o in orders}

    def _eligible(self, accounts: list[TokenAccount], cfg: AppConfig) -> list[TokenAccount]:
        now = time.monotonic()
        quote_mints = set(cfg.providers.quote_mints) | {cfg.execution.quote_mint}
        return [
            a
            for a in accounts
            if a.amount_raw == 0 and a.mint not in quote_mints and self._backoff.get(a.address, (0.0, 0))[0] <= now
        ]

    async def run_once(self) -> CloseReport:
        cfg = self._config()
        report = CloseReport()
        if not cfg.execution.close_empty_token_accounts:
            report.outcome = "disabled"
            return report
        if not self.mode.live_allowed:
            report.outcome = "not_armed"  # no transaction is ever sent while disarmed
            return report
        owner = self.live.wallet
        candidates = self._eligible(await self.chain.get_token_accounts(owner), cfg)
        report.candidates = len(candidates)
        if not candidates:
            return report
        busy = await self._busy_mints()
        free = [a for a in candidates if a.mint not in busy]
        report.skipped_busy = len(candidates) - len(free)
        batch = free[:MAX_ACCOUNTS_PER_TX]
        if not batch:
            return report
        async with contextlib.AsyncExitStack() as stack:
            for mint in sorted({a.mint for a in batch}):  # fixed order: no lock-ordering deadlock
                await stack.enter_async_context(self.locks.hold(mint))
            busy = await self._busy_mints()  # re-check under the locks
            batch = [a for a in batch if a.mint not in busy]
            if not batch:
                return report
            await self._close(owner, batch, report)
        return report

    async def _close(self, owner: str, batch: list[TokenAccount], report: CloseReport) -> None:
        report.mints = [a.mint for a in batch]
        try:
            blockhash, last_valid = await self.chain.get_latest_blockhash()
            tx_bytes = build_close_accounts_tx(owner, batch, blockhash)
            intent = SignIntent(
                client_order_id=f"close-accounts-{uuid.uuid4().hex[:16]}",
                purpose="maintenance",
                input_mint="",
                output_mint="",
                amount_in_raw=0,
                notional_usd=0.0,
            )
            signed = await self.live.signer.sign(tx_bytes, intent)
            report.signature = signed.signature
            state = await self._send_and_confirm(signed.tx_bytes, signed.signature, last_valid)
        except (CopyTraderError, SecurityError) as exc:
            log.warning("token_account_close_failed", accounts=len(batch), error=str(exc))
            self._fail(batch)
            report.outcome = "failed"
            return
        if state == "confirmed":
            rent = sum(a.lamports for a in batch) / 1e9
            report.closed, report.rent_recovered_sol, report.outcome = len(batch), rent, "closed"
            metrics.RENT_RECOVERED.inc(rent)
            for a in batch:
                self._backoff.pop(a.address, None)
            async with self.db.session() as s:
                await EventLogRepo(s).add(
                    "execution",
                    "token_accounts_closed",
                    data={"accounts": len(batch), "mints": report.mints, "rent_sol": rent, "tx": signed.signature},
                )
            log.info("token_accounts_closed", accounts=len(batch), rent_sol=round(rent, 6))
        elif state == "pending":
            report.outcome = "pending"  # the next run sees whether the accounts are gone
        else:
            log.warning("token_account_close_tx_failed", state=state, accounts=len(batch))
            self._fail(batch)
            report.outcome = "failed"

    async def _send_and_confirm(self, tx_bytes: bytes, signature: str, last_valid: int) -> str:
        cfg = self._config().execution
        deadline = time.monotonic() + cfg.confirm_timeout_seconds
        last_send = 0.0
        while True:
            now = time.monotonic()
            if now - last_send >= cfg.rebroadcast_interval_ms / 1000:
                try:
                    # Preflight on: closing is not urgent and a doomed tx should not pay fees.
                    await self.chain.send_raw_transaction(tx_bytes, skip_preflight=False)
                except ProviderError as exc:
                    if not exc.retryable:
                        return f"failed:{exc}"
                except CopyTraderError:
                    pass
                last_send = now
            state = await self.live.signature_state(signature, last_valid)
            if state != "pending":
                return state
            if now > deadline:
                return "pending"
            await asyncio.sleep(0.5)

    def _fail(self, batch: list[TokenAccount]) -> None:
        now = time.monotonic()
        for a in batch:
            _, failures = self._backoff.get(a.address, (0.0, 0))
            failures += 1
            delay = min(MAX_FAILURE_BACKOFF_SECONDS, FAILURE_BACKOFF_SECONDS * failures)
            self._backoff[a.address] = (now + delay, failures)

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except Exception:
                metrics.ERRORS.labels(component="token_accounts").inc()
                log.exception("token_account_janitor_failed")
            with contextlib.suppress(TimeoutError):
                interval = self._config().execution.close_accounts_interval_seconds
                await asyncio.wait_for(self._stopped.wait(), timeout=interval)

    async def stop(self) -> None:
        self._stopped.set()
