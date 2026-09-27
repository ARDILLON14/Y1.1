"""Signer abstraction.

* ``RemoteSigner`` (recommended): the trading process never sees the key; it
  sends the unsigned transaction + declared intent to the signer service over
  an HMAC-authenticated channel on an internal network.
* ``LocalSigner``: loads the encrypted keystore in-process. Simpler, but the
  key lives in the same process as the network-facing code. For development
  or single-host setups where the operator accepts that trade-off.
* ``NullSigner``: used in analysis/paper modes; always refuses.

Every signer re-runs ``SignerPolicy`` locally before signing.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from copytrader.core.errors import SecurityError, SignerPolicyViolation
from copytrader.security.hmac_auth import sign_request
from copytrader.security.signer_policy import SignerPolicy, SignIntent


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    tx_bytes: bytes
    signature: str


class Signer(Protocol):
    async def public_key(self) -> str: ...

    async def sign(self, tx_bytes: bytes, intent: SignIntent) -> SignedTransaction: ...

    async def healthy(self) -> bool: ...


class NullSigner:
    async def public_key(self) -> str:
        raise SecurityError("no signer configured")

    async def sign(self, tx_bytes: bytes, intent: SignIntent) -> SignedTransaction:
        raise SecurityError("no signer configured (security.signer_mode = none)")

    async def healthy(self) -> bool:
        return False


def sign_with_keypair(keypair: Any, tx_bytes: bytes, policy: SignerPolicy,
                      intent: SignIntent) -> SignedTransaction:
    from solders.transaction import VersionedTransaction

    tx = VersionedTransaction.from_bytes(tx_bytes)
    policy.authorize(tx, intent)
    signed = VersionedTransaction(tx.message, [keypair])
    return SignedTransaction(tx_bytes=bytes(signed), signature=str(signed.signatures[0]))


class LocalSigner:
    def __init__(self, keypair: Any, policy: SignerPolicy) -> None:
        self._keypair = keypair
        self._policy = policy
        if str(keypair.pubkey()) != policy.owner:
            raise SecurityError("policy owner does not match keypair")

    async def public_key(self) -> str:
        return str(self._keypair.pubkey())

    async def sign(self, tx_bytes: bytes, intent: SignIntent) -> SignedTransaction:
        return sign_with_keypair(self._keypair, tx_bytes, self._policy, intent)

    async def healthy(self) -> bool:
        return True


class RemoteSigner:
    def __init__(self, base_url: str, hmac_key: bytes, *, expected_pubkey: str | None,
                 timeout: float = 5.0, client: httpx.AsyncClient | None = None,
                 local_policy: SignerPolicy | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._key = hmac_key
        self._expected = expected_pubkey
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._policy = local_policy

    async def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload or {}, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", **sign_request(self._key, method, path, body)}
        try:
            resp = await self._client.request(method, self._base + path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise SecurityError(f"signer unreachable: {type(exc).__name__}") from exc
        if resp.status_code == 403:
            raise SignerPolicyViolation(resp.json().get("detail", "policy violation"))
        if resp.status_code != 200:
            raise SecurityError(f"signer error HTTP {resp.status_code}")
        return resp.json()

    async def public_key(self) -> str:
        data = await self._call("GET", "/v1/pubkey")
        pubkey = str(data["public_key"])
        if self._expected and pubkey != self._expected:
            raise SecurityError("signer public key does not match execution.wallet_public_key")
        return pubkey

    async def sign(self, tx_bytes: bytes, intent: SignIntent) -> SignedTransaction:
        if self._policy is not None:
            # Fail fast locally; the service enforces its own copy of the policy anyway.
            from solders.transaction import VersionedTransaction

            violations = self._policy.inspect(VersionedTransaction.from_bytes(tx_bytes), intent)
            if violations:
                raise SignerPolicyViolation("; ".join(violations))
        data = await self._call("POST", "/v1/sign", {
            "tx": base64.b64encode(tx_bytes).decode(), "intent": intent.to_dict()})
        signed = base64.b64decode(data["tx"])
        from solders.transaction import VersionedTransaction

        parsed = VersionedTransaction.from_bytes(signed)
        # The service must return the *same* message we asked it to sign.
        if bytes(parsed.message) != bytes(VersionedTransaction.from_bytes(tx_bytes).message):
            raise SecurityError("signer returned a different transaction")
        return SignedTransaction(tx_bytes=signed, signature=str(parsed.signatures[0]))

    async def healthy(self) -> bool:
        try:
            await self.public_key()
            return True
        except Exception:
            return False
