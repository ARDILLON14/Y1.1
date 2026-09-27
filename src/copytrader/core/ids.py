"""Deterministic identifiers used for idempotency.

The same logical event must always map to the same key so that database
UNIQUE constraints reject duplicates produced by reconnections, retries or
restarts.
"""

from __future__ import annotations

import hashlib
import secrets


def _digest(*parts: object, length: int = 40) -> str:
    raw = "|".join(str(p) for p in parts).encode()
    return hashlib.sha256(raw).hexdigest()[:length]


def signal_key(chain: str, wallet: str, signature: str, token_mint: str, side: str) -> str:
    return _digest("signal", chain, wallet, signature, token_mint, side)


def entry_order_id(signal_key_value: str) -> str:
    """One entry order per signal, ever."""
    return "ent-" + _digest("entry", signal_key_value, length=36)


def exit_order_id(position_id: int, trigger: str, sequence: int) -> str:
    """Exit orders are keyed by position, trigger and a per-position sequence."""
    return "exi-" + _digest("exit", position_id, trigger, sequence, length=36)


def new_trace_id() -> str:
    return secrets.token_hex(8)
