"""DexScreener token market data (price, liquidity, market cap, age)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from copytrader.core.clock import from_unix
from copytrader.providers.interfaces import MarketData
from copytrader.resilience.http import ResilientHttp


def _f(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


class DexScreenerClient:
    BATCH = 30

    def __init__(self, http: ResilientHttp, base_url: str, chain: str = "solana") -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.chain = chain

    async def token_market(self, mints: Sequence[str]) -> dict[str, MarketData]:
        out: dict[str, MarketData] = {}
        unique = list(dict.fromkeys(mints))
        for i in range(0, len(unique), self.BATCH):
            chunk = unique[i:i + self.BATCH]
            data = await self.http.get_json(f"{self.base_url}/tokens/v1/{self.chain}/{','.join(chunk)}")
            pairs = data if isinstance(data, list) else (data or {}).get("pairs") or []
            out.update(aggregate_pairs(pairs, set(chunk)))
        return out


def aggregate_pairs(pairs: list[dict[str, Any]], mints: set[str]) -> dict[str, MarketData]:
    """Pick, per token, the most liquid pair where the token is the *base* asset.

    Using the deepest pool (not the sum) is deliberately conservative: our
    slippage depends on the pool we actually trade against. Token age is the
    creation time of its oldest pool.
    """
    best: dict[str, dict[str, Any]] = {}
    oldest: dict[str, float] = {}
    for pair in pairs:
        base = (pair.get("baseToken") or {}).get("address")
        if base not in mints:
            continue
        liq = _f((pair.get("liquidity") or {}).get("usd")) or 0.0
        created = _f(pair.get("pairCreatedAt"))
        if created:
            oldest[base] = min(oldest.get(base, created), created)
        current = best.get(base)
        if current is None or liq > (_f((current.get("liquidity") or {}).get("usd")) or 0.0):
            best[base] = pair
    result: dict[str, MarketData] = {}
    for mint, pair in best.items():
        base = pair.get("baseToken") or {}
        changes = {k: v for k, v in ((k, _f(v)) for k, v in (pair.get("priceChange") or {}).items())
                   if v is not None}
        created_ms = oldest.get(mint)
        result[mint] = MarketData(
            mint=mint,
            symbol=base.get("symbol"),
            name=base.get("name"),
            price_usd=_f(pair.get("priceUsd")),
            liquidity_usd=_f((pair.get("liquidity") or {}).get("usd")),
            market_cap_usd=_f(pair.get("marketCap")) or _f(pair.get("fdv")),
            fdv_usd=_f(pair.get("fdv")),
            volume_24h_usd=_f((pair.get("volume") or {}).get("h24")),
            pair_created_at=from_unix(created_ms / 1000) if created_ms else None,
            price_change_pct=changes,
            dex=pair.get("dexId"),
            pair_address=pair.get("pairAddress"),
        )
    return result
