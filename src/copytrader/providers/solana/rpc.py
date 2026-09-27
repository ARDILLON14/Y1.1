"""Solana JSON-RPC client (HTTP) on top of ``ResilientHttp``."""

from __future__ import annotations

import base64
import itertools
from collections.abc import Sequence
from typing import Any

from copytrader.core.errors import ProviderError
from copytrader.providers.solana.constants import SOL_MINT, TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from copytrader.resilience.http import ResilientHttp
from copytrader.resilience.retry import RetryPolicy

_NO_RETRY = RetryPolicy(max_attempts=1)


class RpcError(ProviderError):
    pass


class SolanaRpc:
    def __init__(self, http: ResilientHttp, url: str, commitment: str = "confirmed") -> None:
        self.http = http
        self.url = url
        self.commitment = commitment
        self._ids = itertools.count(1)

    async def call(self, method: str, params: list[Any] | None = None, *, retry: RetryPolicy | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        data = await self.http.post_json(self.url, json=payload, retry=retry)
        if not isinstance(data, dict):
            raise RpcError(f"{method}: invalid response", provider="solana_rpc")
        if data.get("error"):
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            message = err.get("message") if isinstance(err, dict) else str(err)
            # -32005 node behind / -32004 block not available / 429-like: retryable
            retryable = code in (-32005, -32004, -32007, -32014, -32603)
            raise RpcError(f"{method}: {message} ({code})", provider="solana_rpc", retryable=retryable)
        return data.get("result")

    # ------------------------------------------------------------------ reads
    async def get_transaction(self, signature: str, *, commitment: str | None = None) -> dict[str, Any] | None:
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": commitment or self.commitment,
                },
            ],
        )

    async def get_signatures_for_address(
        self, address: str, *, before: str | None = None, until: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        opts: dict[str, Any] = {"limit": min(limit, 1000), "commitment": self.commitment}
        if before:
            opts["before"] = before
        if until:
            opts["until"] = until
        return list(await self.call("getSignaturesForAddress", [address, opts]) or [])

    async def get_account_info(self, address: str) -> dict[str, Any] | None:
        result = await self.call("getAccountInfo", [address, {"encoding": "jsonParsed", "commitment": self.commitment}])
        return (result or {}).get("value")

    async def get_balance(self, address: str) -> int:
        result = await self.call("getBalance", [address, {"commitment": self.commitment}])
        return int((result or {}).get("value", 0))

    async def get_token_balances(self, owner: str) -> dict[str, int]:
        """Raw token balances by mint for all token accounts of ``owner``."""
        balances: dict[str, int] = {}
        for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            result = await self.call(
                "getTokenAccountsByOwner",
                [owner, {"programId": program}, {"encoding": "jsonParsed", "commitment": self.commitment}],
            )
            for acc in (result or {}).get("value", []):
                info = acc["account"]["data"]["parsed"]["info"]
                mint = info["mint"]
                balances[mint] = balances.get(mint, 0) + int(info["tokenAmount"]["amount"])
        balances.pop(SOL_MINT, None)
        return balances

    async def get_block_height(self) -> int:
        return int(await self.call("getBlockHeight", [{"commitment": self.commitment}]))

    async def get_slot(self) -> int:
        return int(await self.call("getSlot", [{"commitment": self.commitment}]))

    async def get_health(self) -> bool:
        try:
            return await self.call("getHealth", retry=_NO_RETRY) == "ok"
        except ProviderError:
            return False

    async def get_signature_statuses(self, signatures: Sequence[str]) -> list[dict[str, Any] | None]:
        result = await self.call("getSignatureStatuses", [list(signatures), {"searchTransactionHistory": True}])
        return list((result or {}).get("value", []))

    # ----------------------------------------------------------------- writes
    async def send_raw_transaction(self, tx_bytes: bytes, *, skip_preflight: bool = True) -> str:
        """Send a signed transaction. Re-sending the same bytes is idempotent on Solana."""
        encoded = base64.b64encode(tx_bytes).decode()
        return str(
            await self.call(
                "sendTransaction",
                [
                    encoded,
                    {
                        "encoding": "base64",
                        "skipPreflight": skip_preflight,
                        "maxRetries": 0,
                        "preflightCommitment": self.commitment,
                    },
                ],
                retry=_NO_RETRY,
            )
        )
