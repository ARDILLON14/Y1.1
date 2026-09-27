"""HMAC request signing between the trading app and the signer service.

Signature = HMAC-SHA256(key, method \\n path \\n timestamp \\n nonce \\n sha256(body)).

Replay protection: the server rejects timestamps outside ``max_skew`` and
remembers every nonce seen inside that window. Key rotation: the server
accepts the current and the previous key simultaneously.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from copytrader.core.errors import AuthError

HEADER_TS = "X-CT-Timestamp"
HEADER_NONCE = "X-CT-Nonce"
HEADER_SIG = "X-CT-Signature"


def _canonical(method: str, path: str, ts: str, nonce: str, body: bytes) -> bytes:
    return "\n".join([method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest()]).encode()


def sign_request(key: bytes, method: str, path: str, body: bytes,
                 now: float | None = None) -> dict[str, str]:
    ts = str(int(now if now is not None else time.time()))
    nonce = secrets.token_hex(16)
    sig = hmac.new(key, _canonical(method, path, ts, nonce, body), hashlib.sha256).hexdigest()
    return {HEADER_TS: ts, HEADER_NONCE: nonce, HEADER_SIG: sig}


@dataclass
class HmacVerifier:
    keys: list[bytes]
    max_skew_seconds: float = 30.0
    clock: Callable[[], float] = time.time
    max_nonces: int = 100_000

    def __post_init__(self) -> None:
        self._nonces: OrderedDict[str, float] = OrderedDict()
        if not self.keys:
            raise AuthError("at least one HMAC key is required")

    def verify(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
        lowered = {k.lower(): v for k, v in headers.items()}
        ts = lowered.get(HEADER_TS.lower())
        nonce = lowered.get(HEADER_NONCE.lower())
        sig = lowered.get(HEADER_SIG.lower())
        if not ts or not nonce or not sig:
            raise AuthError("missing authentication headers")
        try:
            ts_value = int(ts)
        except ValueError as exc:
            raise AuthError("bad timestamp") from exc
        now = self.clock()
        if abs(now - ts_value) > self.max_skew_seconds:
            raise AuthError("timestamp outside allowed window")
        if len(nonce) < 16 or len(nonce) > 128:
            raise AuthError("bad nonce")
        message = _canonical(method, path, ts, nonce, body)
        if not any(hmac.compare_digest(hmac.new(k, message, hashlib.sha256).hexdigest(), sig)
                   for k in self.keys):
            raise AuthError("bad signature")
        self._purge(now)
        if nonce in self._nonces:
            raise AuthError("replayed request")
        self._nonces[nonce] = now

    def _purge(self, now: float) -> None:
        cutoff = now - 2 * self.max_skew_seconds
        while self._nonces:
            first_nonce, seen = next(iter(self._nonces.items()))
            if seen >= cutoff and len(self._nonces) < self.max_nonces:
                break
            self._nonces.pop(first_nonce)
