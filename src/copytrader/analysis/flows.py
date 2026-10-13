"""Where a wallet's tokens come from and where they go (diagnostic).

The analysis only counts trades it can reconstruct: a token bought with SOL or a
stable and later sold for them. A sell whose purchase it never saw has no cost
basis and is left out ("ventas sin compra"). When a wallet has many of those,
this module tells why, transaction by transaction:

* ``buy`` / ``sell``: swaps the parser recognised;
* ``created``: the token was created in that very transaction (the wallet is its
  developer; its profits are not copyable);
* ``transfer_in`` / ``transfer_out``: tokens moved to/from another wallet without
  paying or receiving SOL (one operator using several wallets, or gifts);
* ``unparsed_buy`` / ``unparsed_sell``: SOL or a stable did move but the parser
  rejected the swap (several tokens at once, unusual routes): a blind spot of
  the bot, worth reporting;
* ``other_in`` / ``other_out``: airdrops, spam, burns.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from copytrader.core.clock import from_unix
from copytrader.core.errors import CopyTraderError, ParseError
from copytrader.core.types import Side
from copytrader.providers.solana.constants import SOL_DUST_LAMPORTS, SOL_MINT, STABLE_MINTS
from copytrader.providers.solana.parser import normalize_transaction, parse_swaps, wallet_balance_deltas

QUOTE_MINTS = (SOL_MINT, *STABLE_MINTS)
MATCHED_TRANSFER = 0.9  # the counterpart moved at least 90 % of the amount
EXAMPLES = 3

KIND_LABELS = {
    "buy": "compras reconocidas",
    "created": "tokens creados por la propia wallet (dev)",
    "transfer_in": "recibidos de otra wallet (sin pagar SOL)",
    "unparsed_buy": "compras NO reconocidas por el bot",
    "other_in": "otros (airdrops, spam)",
    "sell": "ventas reconocidas",
    "transfer_out": "enviados a otra wallet (sin cobrar SOL)",
    "unparsed_sell": "ventas NO reconocidas por el bot",
    "other_out": "otros (quemados, spam)",
}


@dataclass(frozen=True, slots=True)
class TokenFlow:
    kind: str
    mint: str
    signature: str
    counterpart: str | None = None  # the other wallet of a transfer


def _owner_deltas(meta: Mapping[str, Any]) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = defaultdict(int)
    for name, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for bal in meta.get(name) or []:
            owner, mint = bal.get("owner"), bal.get("mint")
            if owner and mint:
                out[(str(owner), str(mint))] += sign * int((bal.get("uiTokenAmount") or {}).get("amount") or 0)
    return out


def classify_token_flows(raw: Mapping[str, Any], wallet: str) -> list[TokenFlow]:
    """Every token (other than SOL and stables) that entered or left ``wallet`` in this transaction."""
    tx = normalize_transaction(raw)
    meta = tx.meta
    if meta.get("err") is not None or wallet not in tx.account_keys:
        return []
    deltas = wallet_balance_deltas(tx, wallet)
    tokens = dict(deltas.tokens)
    sol = deltas.sol_delta
    wsol = tokens.pop(SOL_MINT, None)
    if wsol is not None:
        sol += wsol.delta_raw
    stables = {m: tokens.pop(m).delta_raw for m in list(tokens) if m in STABLE_MINTS}
    quote_out = sol < -SOL_DUST_LAMPORTS or any(d < 0 for d in stables.values())
    quote_in = sol > SOL_DUST_LAMPORTS or any(d > 0 for d in stables.values())
    try:
        swaps = parse_swaps(raw, wallet, sol_price_usd=None, quote_mints=QUOTE_MINTS)
    except ParseError:
        swaps = []
    bought = {s.token_mint for s in swaps if s.side is Side.BUY}
    sold = {s.token_mint for s in swaps if s.side is Side.SELL}
    owners = _owner_deltas(meta)
    pre_mints = {str(b.get("mint")) for b in meta.get("preTokenBalances") or []}

    def counterpart(mint: str, amount: int, sign: int) -> str | None:
        """The other wallet whose balance of ``mint`` moved the opposite way by about ``amount``."""
        moves = [(o, sign * d) for (o, m), d in owners.items() if m == mint and o != wallet and sign * d > 0]
        if not moves or sum(x for _, x in moves) < MATCHED_TRANSFER * amount:
            return None
        return max(moves, key=lambda mv: mv[1])[0]

    flows: list[TokenFlow] = []
    for mint, td in tokens.items():
        d = td.delta_raw
        if d > 0:
            if quote_out and mint not in pre_mints and any(m == mint and o != wallet for (o, m) in owners):
                kind, other = "created", None  # mint born in this transaction (dev create + first buy)
            elif mint in bought:
                kind, other = "buy", None
            elif quote_out:
                kind, other = "unparsed_buy", None
            else:
                other = counterpart(mint, d, -1)
                kind = "transfer_in" if other else "other_in"
        elif d < 0:
            if mint in sold:
                kind, other = "sell", None
            elif quote_in:
                kind, other = "unparsed_sell", None
            else:
                other = counterpart(mint, -d, 1)
                kind = "transfer_out" if other else "other_out"
        else:
            continue
        flows.append(TokenFlow(kind, mint, tx.signature, other))
    return flows


@dataclass(slots=True)
class FlowReport:
    wallet: str
    reviewed: int = 0
    failed: int = 0
    first: datetime | None = None
    last: datetime | None = None
    kinds: Counter[str] = field(default_factory=Counter)
    counterparts: Counter[str] = field(default_factory=Counter)
    examples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def add(self, flows: Iterable[TokenFlow], when: datetime | None) -> None:
        self.reviewed += 1
        if when is not None:
            self.first = min(self.first or when, when)
            self.last = max(self.last or when, when)
        for f in flows:
            self.kinds[f.kind] += 1
            if f.counterpart:
                self.counterparts[f.counterpart] += 1
            if len(self.examples[f.kind]) < EXAMPLES and f.signature not in self.examples[f.kind]:
                self.examples[f.kind].append(f.signature)

    def render(self) -> str:
        span = ""
        if self.first and self.last:
            days = (self.last - self.first).total_seconds() / 86400
            span = f" ({self.first:%d/%m %H:%M} → {self.last:%d/%m %H:%M}, {days:.1f} días)"
        lines = [f"Wallet {self.wallet}", f"Transacciones revisadas: {self.reviewed}{span}"]
        if self.failed:
            lines.append(f"No se pudieron descargar: {self.failed}")
        for title, kinds in (
            ("Entradas de tokens", ("buy", "created", "transfer_in", "unparsed_buy", "other_in")),
            ("Salidas de tokens", ("sell", "transfer_out", "unparsed_sell", "other_out")),
        ):
            lines.append(f"{title}:")
            for k in kinds:
                lines.append(f"  {self.kinds.get(k, 0):>5}  {KIND_LABELS[k]}")
        if self.counterparts:
            top = ", ".join(f"{a[:4]}…{a[-4:]} ({n})" for a, n in self.counterparts.most_common(3))
            lines.append(f"Wallets con las que se pasa tokens: {top}")
        for k in ("unparsed_buy", "unparsed_sell"):
            if self.examples.get(k):
                lines.append(f"Ejemplos de {KIND_LABELS[k]} (para revisarlos):")
                lines.extend(f"  https://solscan.io/tx/{sig}" for sig in self.examples[k])
        return "\n".join(lines)


class _Rpc(Protocol):
    async def get_transaction(self, signature: str) -> dict[str, Any] | None: ...

    async def get_signatures_for_address(
        self, address: str, *, before: str | None = None, until: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class Origin:
    """How a token the wallet sold had entered it."""

    mint: str
    kind: str  # a TokenFlow kind, or "not_found"
    when: datetime | None = None
    signature: str | None = None
    counterpart: str | None = None


async def trace_origin(rpc: _Rpc, wallet: str, mint: str, sell_signature: str, *, max_txs: int = 4) -> Origin:
    """Follow the wallet's token account used in ``sell_signature`` back to its first inflow."""
    try:
        sell = await rpc.get_transaction(sell_signature)
        if not sell:
            return Origin(mint, "not_found")
        tx = normalize_transaction(sell)
        account = next(
            (
                tx.account_keys[int(b["accountIndex"])]
                for b in tx.meta.get("preTokenBalances") or []
                if b.get("owner") == wallet and b.get("mint") == mint and int(b["accountIndex"]) < len(tx.account_keys)
            ),
            None,
        )
        if account is None:
            return Origin(mint, "not_found")
        sigs = [s for s in await rpc.get_signatures_for_address(account, limit=1000) if s.get("err") is None]
        for item in reversed(sigs[-max_txs:]):  # oldest first: account creation and first inflow
            raw = await rpc.get_transaction(item["signature"])
            if not raw:
                continue
            for flow in classify_token_flows(raw, wallet):
                if flow.mint == mint and flow.kind in ("buy", "created", "transfer_in", "unparsed_buy", "other_in"):
                    when = from_unix(raw["blockTime"]) if raw.get("blockTime") else None
                    return Origin(mint, flow.kind, when, flow.signature, flow.counterpart)
    except (CopyTraderError, KeyError, ValueError):
        pass
    return Origin(mint, "not_found")


ORIGIN_LABELS = {
    "before_window": "comprados antes del periodo analizado",
    "buy_not_stored": "compras que el bot NO guardó (fallo del bot)",
    "buy_stored": "compras guardadas",
    "transfer_in": "recibidos de otra wallet sin pagar",
    "created": "creados por la propia wallet",
    "unparsed_buy": "compras no reconocidas (fallo del bot)",
    "other_in": "airdrops/otros",
    "not_found": "origen no encontrado",
}


def describe_origin(origin: Origin, *, analysed_since: datetime, stored: set[str]) -> tuple[str, str]:
    """(category, one line in plain Spanish). ``stored``: signatures the bot has in its database."""
    when = f"{origin.when:%d/%m}" if origin.when else "?"
    if origin.kind == "buy":
        if origin.when and origin.when < analysed_since:
            return "before_window", f"comprado el {when}, antes del periodo analizado"
        if origin.signature in stored:
            return "buy_stored", f"comprado el {when} (la compra sí está guardada)"
        return (
            "buy_not_stored",
            f"comprado el {when}: compra que el bot NO guardó · https://solscan.io/tx/{origin.signature}",
        )
    if origin.kind == "transfer_in":
        who = f"{origin.counterpart[:4]}…{origin.counterpart[-4:]}" if origin.counterpart else "otra wallet"
        return "transfer_in", f"recibido de {who} el {when} sin pagar"
    if origin.kind == "created":
        return "created", f"token creado por la propia wallet el {when}"
    if origin.kind == "unparsed_buy":
        return "unparsed_buy", f"compra NO reconocida por el bot el {when} · https://solscan.io/tx/{origin.signature}"
    if origin.kind == "other_in":
        return "other_in", f"airdrop/otro el {when}"
    return "not_found", "origen no encontrado (cuenta con mucha actividad o cerrada)"
