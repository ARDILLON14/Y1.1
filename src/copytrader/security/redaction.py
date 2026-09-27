"""Scrub secrets from any text/structure before it leaves the process.

Applied to: every log record (structlog processor), every notification
(Telegram/Discord), every API error message.

Two layers:
1. Exact values: every configured secret (API keys, tokens, passphrase, the
   bot keypair in the signer process) is registered and replaced verbatim.
2. Patterns: key-like shapes that should never appear anyway (keypair JSON
   arrays, hex private keys, BIP39-like mnemonics, bot tokens, webhook URLs,
   ``api-key=`` query params) and sensitive dict keys.

Note: 64-byte base58 strings are *not* pattern-redacted because Solana
transaction signatures share that shape and are needed for auditing; the
actual secret key is covered by layer 1.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "password",
    "passphrase",
    "secret",
    "secret_key",
    "private_key",
    "privkey",
    "seed",
    "mnemonic",
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "cookie",
    "set-cookie",
    "bot_token",
    "webhook_url",
    "hmac_key",
    "keypair",
    "x-signature",
    "totp",
    "totp_secret",
    "session",
    "csrf_token",
    "x-csrf-token",
    "x-api-key",
}
_SENSITIVE_SUFFIXES = ("_secret", "_password", "_passphrase", "_private_key", "_api_key", "_hmac_key", "_bot_token")

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Solana keypair file format: JSON array of 64 small ints
    (re.compile(r"\[\s*(?:\d{1,3}\s*,\s*){63}\d{1,3}\s*\]"), REDACTED),
    # 32/64-byte hex strings (private keys / seeds)
    (re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}(?:[0-9a-fA-F]{64})?\b"), REDACTED),
    # Telegram bot token
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), REDACTED),
    # Discord webhook
    (re.compile(r"(https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/)[\w-]+"), r"\1" + REDACTED),
    # api-key / token query parameters
    (re.compile(r"(?i)([?&](?:api[-_]?key|token|key|secret)=)[^&\s\"']+"), r"\1" + REDACTED),
    # Bearer tokens
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1" + REDACTED),
]
_MNEMONIC = re.compile(r"\b(?:[a-z]{3,8}\s){11}(?:[a-z]{3,8}\s){0,12}[a-z]{3,8}\b")


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return k in _SENSITIVE_KEYS or k.endswith(_SENSITIVE_SUFFIXES)


class Redactor:
    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()
        self._compiled: re.Pattern[str] | None = None

    def register(self, values: Iterable[str | None]) -> None:
        with self._lock:
            for v in values:
                if v and len(v) >= 6:
                    self._values.add(v)
            if self._values:
                ordered = sorted(self._values, key=len, reverse=True)
                self._compiled = re.compile("|".join(re.escape(v) for v in ordered))

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._compiled = None

    def text(self, value: str) -> str:
        if not value:
            return value
        compiled = self._compiled
        if compiled is not None:
            value = compiled.sub(REDACTED, value)
        for pattern, repl in _PATTERNS:
            value = pattern.sub(repl, value)
        return _mnemonic_scrub(value)

    def data(self, value: Any, _depth: int = 0) -> Any:
        if _depth > 8:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {
                k: (
                    REDACTED
                    if isinstance(k, str) and _is_sensitive_key(k) and v not in (None, "")
                    else self.data(v, _depth + 1)
                )
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            if len(value) == 64 and all(isinstance(x, int) and 0 <= x < 256 for x in value):
                return REDACTED  # raw keypair bytes
            items = [self.data(v, _depth + 1) for v in value]
            return type(value)(items) if isinstance(value, tuple) else items
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        return value


def _mnemonic_scrub(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        words = match.group(0).split()
        return REDACTED if len(words) in (12, 15, 18, 21, 24) else match.group(0)

    return _MNEMONIC.sub(repl, text)


# Process-wide instance used by logging and notifiers.
REDACTOR = Redactor()


def redact(value: Any) -> Any:
    return REDACTOR.data(value)
