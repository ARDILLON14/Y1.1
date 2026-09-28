"""DEX-agnostic swap parser based on the wallet's balance deltas.

Instead of decoding every DEX's instruction layout (Jupiter, Raydium,
Pump.fun, Orca, Meteora... each with versions), we look at what actually
changed for the wallet in ``meta``:

* SOL: ``postBalances - preBalances`` for the wallet's account index, with
  the transaction fee, Jito tips and token-account rent added back;
* tokens: ``postTokenBalances - preTokenBalances`` for accounts owned by the
  wallet, aggregated per mint (WSOL folded into SOL).

A swap is then "quote asset out, one token in" (BUY) or the reverse (SELL).
Anything ambiguous (several tokens changing at once, no quote movement) is
rejected explicitly instead of guessed — a wrong parse must never produce a
copy signal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from copytrader.core.clock import from_unix
from copytrader.core.errors import ParseError
from copytrader.core.models import SwapEvent
from copytrader.core.types import Side, TxSource
from copytrader.providers.solana.constants import (
    AGGREGATORS,
    DEX_PROGRAMS,
    JITO_TIP_ACCOUNTS,
    LAMPORTS_PER_SOL,
    SOL_DUST_LAMPORTS,
    SOL_MINT,
    STABLE_MINTS,
    TOKEN_ACCOUNT_RENT_LAMPORTS,
)


@dataclass(frozen=True, slots=True)
class NormalizedTx:
    signature: str
    slot: int
    block_time: datetime | None
    meta: Mapping[str, Any]
    account_keys: list[str]


def normalize_transaction(raw: Mapping[str, Any]) -> NormalizedTx:
    """Accept ``getTransaction`` results and Helius ``transactionNotification`` results."""
    if not isinstance(raw, Mapping):
        raise ParseError("transaction payload is not an object")
    inner = raw.get("transaction")
    if isinstance(inner, Mapping) and "meta" in inner and "transaction" in inner:
        tx, meta = inner["transaction"], inner["meta"]
        block_time = inner.get("blockTime") or raw.get("blockTime")
    else:
        tx, meta = inner, raw.get("meta")
        block_time = raw.get("blockTime")
    if not isinstance(tx, Mapping) or not isinstance(meta, Mapping):
        raise ParseError("missing transaction or meta")
    signatures = tx.get("signatures") or []
    signature = raw.get("signature") or (signatures[0] if signatures else None)
    if not signature:
        raise ParseError("missing signature")
    message = tx.get("message") or {}
    raw_keys = message.get("accountKeys") or []
    if raw_keys and isinstance(raw_keys[0], Mapping):
        keys = [str(k["pubkey"]) for k in raw_keys]
    else:
        loaded = meta.get("loadedAddresses") or {}
        keys = [str(k) for k in raw_keys] + list(loaded.get("writable", [])) + list(loaded.get("readonly", []))
    slot = raw.get("slot")
    return NormalizedTx(
        signature=str(signature),
        slot=int(slot) if slot is not None else 0,
        block_time=from_unix(block_time) if block_time else None,
        meta=meta,
        account_keys=keys,
    )


def _dex_label(keys: Iterable[str]) -> str:
    labels = [DEX_PROGRAMS[k] for k in keys if k in DEX_PROGRAMS]
    if not labels:
        return "unknown"
    aggregators = [lbl for lbl in labels if lbl in AGGREGATORS]
    return aggregators[0] if aggregators else labels[0]


@dataclass(slots=True)
class _TokenDelta:
    mint: str
    decimals: int
    pre_raw: int = 0
    post_raw: int = 0

    @property
    def delta_raw(self) -> int:
        return self.post_raw - self.pre_raw

    def ui(self, raw: int) -> float:
        return raw / (10**self.decimals)


def created_token_accounts(raw: Mapping[str, Any], wallet: str) -> list[str]:
    """Mints of the token accounts owned by ``wallet`` that this transaction created."""
    meta = normalize_transaction(raw).meta
    pre = {int(b.get("accountIndex", -1)) for b in meta.get("preTokenBalances") or [] if b.get("owner") == wallet}
    return [
        str(b["mint"])
        for b in meta.get("postTokenBalances") or []
        if b.get("owner") == wallet and int(b.get("accountIndex", -1)) not in pre
    ]


def parse_swaps(
    raw: Mapping[str, Any],
    wallet: str,
    *,
    sol_price_usd: float | None,
    quote_mints: Iterable[str] = (SOL_MINT, *STABLE_MINTS),
    token_price_usd: Callable[[str], float | None] | None = None,
    source: TxSource = TxSource.STREAM,
    detected_at: datetime | None = None,
    fallback_time: datetime | None = None,
) -> list[SwapEvent]:
    """Return the swaps ``wallet`` performed in this transaction (usually 0 or 1)."""
    tx = normalize_transaction(raw)
    meta = tx.meta
    if meta.get("err") is not None:
        return []
    keys = tx.account_keys
    if wallet not in keys:
        return []
    block_time = tx.block_time or fallback_time or detected_at
    if block_time is None:
        raise ParseError("transaction without blockTime and no fallback time")
    idx = keys.index(wallet)
    pre_bal = meta.get("preBalances") or []
    post_bal = meta.get("postBalances") or []
    if idx >= len(pre_bal) or idx >= len(post_bal):
        raise ParseError("balance arrays shorter than account keys")

    fee = int(meta.get("fee") or 0)
    sol_delta = int(post_bal[idx]) - int(pre_bal[idx])
    fee_sol = 0.0
    if idx == 0:  # wallet is fee payer: fee, priority fee and tips are costs, not swap flow
        sol_delta += fee
        fee_sol = fee / LAMPORTS_PER_SOL
        for j, key in enumerate(keys):
            if key in JITO_TIP_ACCOUNTS and j < len(pre_bal) and j < len(post_bal):
                tip = int(post_bal[j]) - int(pre_bal[j])
                if tip > 0:
                    sol_delta += tip
                    fee_sol += tip / LAMPORTS_PER_SOL

    tokens: dict[str, _TokenDelta] = {}
    pre_accounts: dict[int, str] = {}
    post_accounts: dict[int, str] = {}
    for field_name, target in (("preTokenBalances", pre_accounts), ("postTokenBalances", post_accounts)):
        for bal in meta.get(field_name) or []:
            if bal.get("owner") != wallet:
                continue
            mint = str(bal["mint"])
            amount = bal.get("uiTokenAmount") or {}
            decimals = int(amount.get("decimals") or 0)
            raw_amount = int(amount.get("amount") or 0)
            entry = tokens.setdefault(mint, _TokenDelta(mint=mint, decimals=decimals))
            if field_name == "preTokenBalances":
                entry.pre_raw += raw_amount
            else:
                entry.post_raw += raw_amount
            target[int(bal.get("accountIndex", -1))] = mint

    # Token-account rent is refundable and not part of the trade price.
    created = [m for i, m in post_accounts.items() if i not in pre_accounts]
    closed = [m for i, m in pre_accounts.items() if i not in post_accounts]
    sol_delta += TOKEN_ACCOUNT_RENT_LAMPORTS * len(created)
    sol_delta -= TOKEN_ACCOUNT_RENT_LAMPORTS * len(closed)

    wsol = tokens.pop(SOL_MINT, None)
    if wsol is not None:
        sol_delta += wsol.delta_raw

    quote_set = set(quote_mints)
    quote_usd: dict[str, tuple[float, float]] = {}  # mint -> (ui delta, usd delta)
    if abs(sol_delta) > SOL_DUST_LAMPORTS:
        ui = sol_delta / LAMPORTS_PER_SOL
        quote_usd[SOL_MINT] = (ui, ui * sol_price_usd if sol_price_usd else float("nan"))
    for mint in list(tokens):
        if mint in quote_set:
            td = tokens.pop(mint)
            if td.delta_raw:
                ui = td.ui(td.delta_raw)
                quote_usd[mint] = (ui, ui if mint in STABLE_MINTS else float("nan"))

    changed = {m: t for m, t in tokens.items() if t.delta_raw != 0}
    if not changed:
        return []
    dex = _dex_label(keys)

    def make(
        td: _TokenDelta, side: Side, quote_mint: str, quote_amount: float, value_usd: float | None
    ) -> SwapEvent | None:
        amount = abs(td.ui(td.delta_raw))
        if amount <= 0 or quote_amount <= 0:
            return None
        price_quote = quote_amount / amount
        valid_usd = value_usd is not None and value_usd == value_usd and value_usd > 0
        return SwapEvent(
            wallet=wallet,
            signature=tx.signature,
            slot=tx.slot,
            block_time=block_time,
            token_mint=td.mint,
            side=side,
            token_amount=amount,
            token_decimals=td.decimals,
            quote_mint=quote_mint,
            quote_amount=quote_amount,
            price_quote=price_quote,
            price_usd=(value_usd / amount) if valid_usd and value_usd else None,
            value_usd=value_usd if valid_usd else None,
            sol_price_usd=sol_price_usd,
            fee_sol=fee_sol,
            dex=dex,
            token_balance_before=td.ui(td.pre_raw),
            token_balance_after=td.ui(td.post_raw),
            source=source,
            detected_at=detected_at,
        )

    if len(changed) == 1:
        td = next(iter(changed.values()))
        if not quote_usd:
            return []  # token moved without quote flow: transfer/airdrop, not a swap
        # Net quote flow (dominant quote by absolute USD, or by UI amount if no prices).
        side = Side.BUY if td.delta_raw > 0 else Side.SELL
        expected_sign = -1 if side is Side.BUY else 1
        flows = [(m, ui, usd) for m, (ui, usd) in quote_usd.items() if ui * expected_sign > 0]
        if not flows:
            return []  # token and quote moved in the same direction: not a swap
        known = [f for f in flows if f[2] == f[2]]
        main = max(known or flows, key=lambda f: abs(f[2]) if f[2] == f[2] else abs(f[1]))
        total_usd = sum(abs(f[2]) for f in known) if known else None
        ev = make(td, side, main[0], abs(main[1]), total_usd)
        return [ev] if ev else []

    if len(changed) == 2 and not quote_usd:
        # token -> token swap: emit SELL of the spent token and BUY of the received one.
        spent = [t for t in changed.values() if t.delta_raw < 0]
        got = [t for t in changed.values() if t.delta_raw > 0]
        if len(spent) != 1 or len(got) != 1 or token_price_usd is None:
            return []
        sell_td, buy_td = spent[0], got[0]
        sell_px = token_price_usd(sell_td.mint)
        if not sell_px:
            return []
        value = abs(sell_td.ui(sell_td.delta_raw)) * sell_px
        out: list[SwapEvent] = []
        for ev in (
            make(sell_td, Side.SELL, buy_td.mint, abs(buy_td.ui(buy_td.delta_raw)), value),
            make(buy_td, Side.BUY, sell_td.mint, abs(sell_td.ui(sell_td.delta_raw)), value),
        ):
            if ev:
                out.append(ev)
        return out

    # Several tokens changed together with quote flow (LP operations, multi-swaps):
    # ambiguous, never guess.
    return []
