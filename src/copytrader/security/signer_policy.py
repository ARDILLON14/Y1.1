"""Independent transaction policy enforced before signing.

The signer does not trust the trading process. Before signing, it decodes the
Solana transaction and refuses anything that could move funds somewhere other
than a swap on behalf of the bot wallet:

* fee payer must be the bot wallet and it must be the only required signer;
* every top-level program must be on an allowlist (Jupiter, compute budget,
  system, SPL token/token-2022, associated token account);
* system transfers only to the bot's own WSOL account (bounded by the declared
  amount) or to a Jito tip account (bounded by the tip cap);
* no top-level SPL ``Transfer``/``Approve``/``SetAuthority``; ``CloseAccount``
  only refunding to the bot wallet;
* priority fee bounded;
* per-transaction and per-day notional caps and a rate limit.

Known limitation (documented in SECURITY.md): the Jupiter route instruction
itself is not decoded, so a compromised trading process could still request a
swap at a bad price *within* the caps. That is why the bot wallet must hold
only the capital it is allowed to trade.
"""

from __future__ import annotations

import json
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copytrader.core.errors import SignerPolicyViolation

SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
JUPITER_V6_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
WSOL_MINT = "So11111111111111111111111111111111111111112"

DEFAULT_ALLOWED_PROGRAMS = frozenset(
    {
        SYSTEM_PROGRAM,
        COMPUTE_BUDGET_PROGRAM,
        TOKEN_PROGRAM,
        TOKEN_2022_PROGRAM,
        ATA_PROGRAM,
        JUPITER_V6_PROGRAM,
    }
)

# Public Jito tip accounts (mainnet).
JITO_TIP_ACCOUNTS = frozenset(
    {
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    }
)

_TOKEN_FORBIDDEN = {
    3: "Transfer",
    4: "Approve",
    6: "SetAuthority",
    7: "MintTo",
    8: "Burn",
    10: "FreezeAccount",
    12: "TransferChecked",
    13: "ApproveChecked",
    14: "MintToChecked",
    15: "BurnChecked",
}
_TOKEN_ALLOWED = {9: "CloseAccount", 17: "SyncNative"}


@dataclass(frozen=True, slots=True)
class SignIntent:
    """What the caller claims the transaction does (checked against the tx)."""

    client_order_id: str
    purpose: str
    input_mint: str
    output_mint: str
    amount_in_raw: int
    notional_usd: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SignIntent:
        return cls(
            client_order_id=str(d["client_order_id"]),
            purpose=str(d["purpose"]),
            input_mint=str(d["input_mint"]),
            output_mint=str(d["output_mint"]),
            amount_in_raw=int(d["amount_in_raw"]),
            notional_usd=float(d["notional_usd"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "purpose": self.purpose,
            "input_mint": self.input_mint,
            "output_mint": self.output_mint,
            "amount_in_raw": self.amount_in_raw,
            "notional_usd": self.notional_usd,
        }


@dataclass
class SignerLimits:
    max_notional_usd_per_tx: float = 100.0
    max_notional_usd_per_day: float = 1000.0
    max_tx_per_minute: int = 20
    max_priority_fee_lamports: int = 5_000_000
    max_tip_lamports: int = 5_000_000
    state_file: str | None = None


@dataclass
class SignerPolicy:
    owner: str
    limits: SignerLimits = field(default_factory=SignerLimits)
    allowed_programs: frozenset[str] = DEFAULT_ALLOWED_PROGRAMS
    _recent: deque[float] = field(default_factory=deque, init=False)
    _day: str = field(default="", init=False)
    _day_total: float = field(default=0.0, init=False)
    _seen_orders: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self._load_state()

    # ------------------------------------------------------------ inspection
    def inspect(self, tx: Any, intent: SignIntent) -> list[str]:
        """Return a list of violations (empty = allowed). Pure w.r.t. counters."""
        from solders.pubkey import Pubkey

        violations: list[str] = []
        msg = tx.message
        keys = [str(k) for k in msg.account_keys]
        if not keys or keys[0] != self.owner:
            violations.append("fee payer is not the bot wallet")
        if msg.header.num_required_signatures != 1:
            violations.append("transaction requires signers other than the bot wallet")

        owner_pk = Pubkey.from_string(self.owner)
        wsol_atas = {
            str(
                Pubkey.find_program_address(
                    [bytes(owner_pk), bytes(Pubkey.from_string(prog)), bytes(Pubkey.from_string(WSOL_MINT))],
                    Pubkey.from_string(ATA_PROGRAM),
                )[0]
            )
            for prog in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)
        }
        cu_limit: int | None = None
        cu_price: int | None = None
        wrapped = 0
        tips = 0
        for ix in msg.instructions:
            if ix.program_id_index >= len(keys):
                violations.append("program id loaded from a lookup table")
                continue
            program = keys[ix.program_id_index]
            data = bytes(ix.data)
            accounts = list(ix.accounts)
            if program not in self.allowed_programs:
                violations.append(f"program not allowed: {program}")
                continue

            def acct(i: int, _accounts: list[int] = accounts) -> str | None:
                if i >= len(_accounts):
                    return None
                idx = _accounts[i]
                return keys[idx] if idx < len(keys) else None  # None = from lookup table

            if program == SYSTEM_PROGRAM:
                if len(data) < 4:
                    violations.append("malformed system instruction")
                    continue
                kind = struct.unpack_from("<I", data, 0)[0]
                if kind != 2 or len(data) < 12:
                    violations.append(f"system instruction {kind} not allowed")
                    continue
                lamports = struct.unpack_from("<Q", data, 4)[0]
                src, dst = acct(0), acct(1)
                if src != self.owner:
                    violations.append("system transfer from a foreign account")
                elif dst in wsol_atas:
                    wrapped += lamports
                elif dst in JITO_TIP_ACCOUNTS:
                    tips += lamports
                else:
                    violations.append(f"system transfer to non-allowed destination {dst}")
            elif program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
                kind = data[0] if data else -1
                if kind in _TOKEN_FORBIDDEN:
                    violations.append(f"token instruction {_TOKEN_FORBIDDEN[kind]} not allowed at top level")
                elif kind not in _TOKEN_ALLOWED:
                    violations.append(f"token instruction {kind} not allowed")
                elif kind == 9 and (acct(1) != self.owner or acct(2) != self.owner):
                    violations.append("CloseAccount must refund to and be authorised by the bot wallet")
            elif program == ATA_PROGRAM:
                if data not in (b"", b"\x00", b"\x01"):
                    violations.append("unsupported associated-token instruction")
                elif acct(0) != self.owner or acct(2) != self.owner:
                    violations.append("ATA creation must be paid for and owned by the bot wallet")
            elif program == COMPUTE_BUDGET_PROGRAM:
                if data[:1] == b"\x02" and len(data) >= 5:
                    cu_limit = struct.unpack_from("<I", data, 1)[0]
                elif data[:1] == b"\x03" and len(data) >= 9:
                    cu_price = struct.unpack_from("<Q", data, 1)[0]

        if cu_price is not None:
            priority_lamports = (cu_limit or 1_400_000) * cu_price // 1_000_000
            if priority_lamports > self.limits.max_priority_fee_lamports:
                violations.append(f"priority fee {priority_lamports} lamports exceeds cap")
        if tips > self.limits.max_tip_lamports:
            violations.append(f"tip {tips} lamports exceeds cap")
        if intent.input_mint == WSOL_MINT:
            if wrapped > intent.amount_in_raw:
                violations.append("wraps more SOL than the declared input amount")
        elif wrapped:
            violations.append("wraps SOL although the declared input is not SOL")
        if intent.notional_usd > self.limits.max_notional_usd_per_tx and intent.purpose == "entry":
            violations.append(f"notional {intent.notional_usd:.2f} exceeds per-transaction cap")
        return violations

    # --------------------------------------------------------------- counters
    def authorize(self, tx: Any, intent: SignIntent, now: float | None = None) -> None:
        """Inspect and apply rate/notional counters; raise on violation."""
        now = time.time() if now is None else now
        violations = self.inspect(tx, intent)
        while self._recent and self._recent[0] < now - 60:
            self._recent.popleft()
        if len(self._recent) >= self.limits.max_tx_per_minute:
            violations.append("rate limit exceeded")
        day = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._day_total = day, 0.0
            self._seen_orders.clear()
        # Re-signing the same order (rebroadcast/retry) does not count twice.
        new_order = intent.client_order_id not in self._seen_orders
        if (
            intent.purpose == "entry"
            and new_order
            and self._day_total + intent.notional_usd > self.limits.max_notional_usd_per_day
        ):
            violations.append("daily notional cap exceeded")
        if violations:
            raise SignerPolicyViolation("; ".join(violations))
        self._recent.append(now)
        if new_order:
            self._seen_orders.add(intent.client_order_id)
            if intent.purpose == "entry":
                self._day_total += intent.notional_usd
        self._save_state()

    def _load_state(self) -> None:
        if not self.limits.state_file:
            return
        p = Path(self.limits.state_file)
        if p.exists():
            try:
                data = json.loads(p.read_text())
                self._day = data.get("day", "")
                self._day_total = float(data.get("total", 0.0))
                self._seen_orders = set(data.get("orders", []))
            except (ValueError, OSError):
                # Corrupt state: be conservative and assume the cap is used up today.
                self._day = datetime.now(UTC).strftime("%Y-%m-%d")
                self._day_total = self.limits.max_notional_usd_per_day

    def _save_state(self) -> None:
        if not self.limits.state_file:
            return
        p = Path(self.limits.state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"day": self._day, "total": self._day_total, "orders": sorted(self._seen_orders)[-5000:]})
        )
        tmp.replace(p)
