"""Jupiter Swap API (quote + swap transaction) and Price API v3."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

from copytrader.core.clock import Clock, SystemClock
from copytrader.core.errors import ProviderError
from copytrader.core.models import Quote
from copytrader.providers.interfaces import BuiltTransaction
from copytrader.resilience.http import ResilientHttp


class JupiterClient:
    def __init__(
        self,
        http: ResilientHttp,
        *,
        quote_url: str,
        swap_url: str,
        price_url: str,
        api_key: str | None = None,
        restrict_intermediate_tokens: bool = True,
        clock: Clock | None = None,
    ) -> None:
        self.http = http
        self.quote_url = quote_url
        self.swap_url = swap_url
        self.price_url = price_url
        self.restrict = restrict_intermediate_tokens
        self.clock = clock or SystemClock()
        self._headers = {"x-api-key": api_key} if api_key else None

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int) -> Quote:
        if amount_raw <= 0:
            raise ValueError("amount must be positive")
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_raw)),
            "slippageBps": str(int(slippage_bps)),
            "swapMode": "ExactIn",
            "restrictIntermediateTokens": "true" if self.restrict else "false",
        }
        data = await self.http.get_json(self.quote_url, params=params, headers=self._headers)
        return parse_quote(data, self.clock)

    async def build_swap(
        self,
        quote: Quote,
        user_public_key: str,
        *,
        priority_max_lamports: int,
        priority_level: str,
        jito_tip_lamports: int = 0,
    ) -> BuiltTransaction:
        if jito_tip_lamports > 0:
            fee: dict[str, Any] = {"jitoTipLamports": int(jito_tip_lamports)}
        else:
            fee = {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": int(priority_max_lamports),
                    "priorityLevel": priority_level,
                }
            }
        body = {
            "quoteResponse": quote.raw,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": fee,
        }
        data = await self.http.post_json(self.swap_url, json=body, headers=self._headers)
        tx_b64 = (data or {}).get("swapTransaction")
        lvbh = (data or {}).get("lastValidBlockHeight")
        if not tx_b64 or lvbh is None:
            raise ProviderError("jupiter: swap response without transaction", provider="jupiter", retryable=False)
        return BuiltTransaction(
            tx_bytes=base64.b64decode(tx_b64),
            last_valid_block_height=int(lvbh),
            raw={k: v for k, v in data.items() if k != "swapTransaction"},
        )

    async def prices_usd(self, mints: Sequence[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        unique = list(dict.fromkeys(mints))
        for i in range(0, len(unique), 50):
            chunk = unique[i : i + 50]
            data = await self.http.get_json(self.price_url, params={"ids": ",".join(chunk)}, headers=self._headers)
            for mint, item in (data or {}).items():
                if not isinstance(item, dict):
                    continue
                price = item.get("usdPrice", item.get("price"))
                if price is None:
                    continue
                try:
                    value = float(price)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    out[mint] = value
        return out


def parse_quote(data: Any, clock: Clock) -> Quote:
    if not isinstance(data, dict) or "outAmount" not in data:
        raise ProviderError("jupiter: no route", provider="jupiter", retryable=False)
    try:
        impact = abs(float(data.get("priceImpactPct") or 0.0))
    except (TypeError, ValueError):
        impact = 0.0
    labels = [str((step.get("swapInfo") or {}).get("label", "?")) for step in data.get("routePlan") or []]
    return Quote(
        input_mint=str(data["inputMint"]),
        output_mint=str(data["outputMint"]),
        in_amount_raw=int(data["inAmount"]),
        out_amount_raw=int(data["outAmount"]),
        min_out_amount_raw=int(data.get("otherAmountThreshold") or data["outAmount"]),
        slippage_bps=int(data.get("slippageBps") or 0),
        price_impact_frac=impact,
        obtained_at=clock.now(),
        route_label=" > ".join(labels),
        raw=data,
    )
