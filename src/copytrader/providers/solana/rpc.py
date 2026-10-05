"""Solana JSON-RPC client (HTTP) on top of ``ResilientHttp``."""

from __future__ import annotations

import base64
import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from copytrader.core.errors import ProviderError
from copytrader.providers.solana.constants import (
    MAX_SUPPORTED_TX_VERSION,
    SOL_MINT,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
)
from copytrader.resilience.http import ResilientHttp
from copytrader.resilience.retry import RetryPolicy

_NO_RETRY = RetryPolicy(max_attempts=1)


class RpcError(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class TokenAccount:
    address: str
    mint: str
    amount_raw: int
    program: str  # SPL Token or Token-2022 program id
    lamports: int = 0


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
                    "maxSupportedTransactionVersion": MAX_SUPPORTED_TX_VERSION,
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

    async def get_token_accounts(self, owner: str) -> list[TokenAccount]:
        """Every SPL Token and Token-2022 account owned by ``owner``."""
        accounts: list[TokenAccount] = []
        for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            result = await self.call(
                "getTokenAccountsByOwner",
                [owner, {"programId": program}, {"encoding": "jsonParsed", "commitment": self.commitment}],
            )
            for acc in (result or {}).get("value", []):
                info = acc["account"]["data"]["parsed"]["info"]
                accounts.append(
                    TokenAccount(
                        address=str(acc["pubkey"]),
                        mint=str(info["mint"]),
                        amount_raw=int(info["tokenAmount"]["amount"]),
                        program=program,
                        lamports=int(acc["account"].get("lamports") or 0),
                    )
                )
        return accounts

    async def get_token_balances(self, owner: str) -> dict[str, int]:
        """Raw token balances by mint for all token accounts of ``owner``."""
        balances: dict[str, int] = {}
        for acc in await self.get_token_accounts(owner):
            balances[acc.mint] = balances.get(acc.mint, 0) + acc.amount_raw
        balances.pop(SOL_MINT, None)
        return balances

    async def get_latest_blockhash(self) -> tuple[str, int]:
        """(blockhash, lastValidBlockHeight)."""
        result = await self.call("getLatestBlockhash", [{"commitment": self.commitment}])
        value = (result or {}).get("value") or {}
        if "blockhash" not in value:
            raise RpcError("getLatestBlockhash without blockhash", provider="solana_rpc")
        return str(value["blockhash"]), int(value["lastValidBlockHeight"])

    async def get_block_height(self) -> int:
        return int(await self.call("getBlockHeight", [{"commitment": self.commitment}]))

    async def get_slot(self) -> int:
        return int(await self.call("getSlot", [{"commitment": self.commitment}]))

    async def get_block_time(self, slot: int) -> int | None:
        """Estimated production time (unix seconds) of a slot; ``None`` if unavailable."""
        result = await self.call("getBlockTime", [slot], retry=_NO_RETRY)
        return int(result) if result is not None else None

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
