"""Merged, cached token information for the pipeline and risk engine.

Market data (fast-changing) and static data (mint authorities, risk report)
have separate TTLs. Provider failures degrade to "unknown" fields, never to
made-up values; the risk checks decide how to treat unknowns (fail-closed for
entries).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TypeVar

import structlog
import yaml

from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.concurrency import TTLCache
from copytrader.core.errors import CopyTraderError
from copytrader.core.models import TokenInfo
from copytrader.providers.interfaces import (
    MarketData,
    MarketDataSource,
    MintData,
    MintInfoSource,
    PriceSource,
    RiskData,
    TokenRiskSource,
)
from copytrader.providers.solana.constants import SOL_MINT, STABLE_MINTS

log = structlog.get_logger(__name__)
_T = TypeVar("_T")


class TokenCategorizer:
    """category = manual map > launchpad heuristics > market-cap bucket."""

    def __init__(self, manual: dict[str, str] | None = None) -> None:
        self.manual = dict(manual or {})

    @classmethod
    def from_file(cls, path: str | None) -> TokenCategorizer:
        if not path or not Path(path).exists():
            return cls()
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        mapping = data.get("tokens", data) if isinstance(data, dict) else {}
        return cls({str(k): str(v) for k, v in mapping.items()})

    def categorize(self, mint: str, market_cap_usd: float | None = None, dex: str | None = None) -> str:
        if mint in self.manual:
            return self.manual[mint]
        if mint in STABLE_MINTS:
            return "stable"
        if mint == SOL_MINT:
            return "major"
        lowered = mint.lower()
        if lowered.endswith("pump") or (dex or "").startswith("pump"):
            return "launchpad:pumpfun"
        if lowered.endswith("bonk"):
            return "launchpad:bonk"
        if market_cap_usd is None:
            return "unknown"
        if market_cap_usd < 1_000_000:
            return "cap:micro"
        if market_cap_usd < 10_000_000:
            return "cap:small"
        if market_cap_usd < 100_000_000:
            return "cap:mid"
        return "cap:large"


class TokenInfoService:
    def __init__(
        self,
        *,
        market: MarketDataSource,
        prices: PriceSource,
        clock: Clock,
        config: Callable[[], AppConfig],
        risk: TokenRiskSource | None = None,
        mint_source: MintInfoSource | None = None,
        categorizer: TokenCategorizer | None = None,
        on_update: Callable[[TokenInfo], object] | None = None,
    ) -> None:
        self.market = market
        self.price_source = prices
        self.risk = risk
        self.mint_source = mint_source
        self.clock = clock
        self._config = config
        self.categorizer = categorizer or TokenCategorizer()
        self._on_update = on_update
        cfg = config().providers
        self._market_cache: TTLCache[str, tuple[MarketData, float, datetime]] = TTLCache(
            max(cfg.token_info_ttl_seconds * 10, 300.0)
        )
        self._static_cache: TTLCache[str, tuple[MintData | None, RiskData | None]] = TTLCache(
            cfg.token_static_ttl_seconds
        )
        self._price_cache: TTLCache[str, tuple[float, float]] = TTLCache(cfg.price_ttl_seconds)
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ prices
    async def prices(self, mints: Sequence[str], *, max_age_seconds: float | None = None) -> dict[str, float]:
        now = self.clock.monotonic()
        max_age = max_age_seconds if max_age_seconds is not None else self._config().providers.price_ttl_seconds
        out: dict[str, float] = {}
        missing: list[str] = []
        for mint in dict.fromkeys(mints):
            cached = self._price_cache.get(mint, now)
            if cached and now - cached[1] <= max_age:
                out[mint] = cached[0]
            else:
                missing.append(mint)
        if missing:
            fetched: dict[str, float] = {}
            try:
                fetched = await self.price_source.prices_usd(missing)
            except CopyTraderError as exc:
                log.warning("price_source_failed", error=str(exc))
            still = [m for m in missing if m not in fetched]
            if still:
                try:
                    market = await self.market.token_market(still)
                    fetched.update({m: d.price_usd for m, d in market.items() if d.price_usd})
                except CopyTraderError as exc:
                    log.warning("market_price_fallback_failed", error=str(exc))
            for mint, price in fetched.items():
                self._price_cache.set(mint, (price, now), now)
                out[mint] = price
        return out

    async def sol_price(self) -> float | None:
        return (await self.prices([SOL_MINT])).get(SOL_MINT)

    # ------------------------------------------------------------------ tokens
    async def get(self, mint: str, *, max_age_seconds: float | None = None) -> TokenInfo:
        result = await self.get_many([mint], max_age_seconds=max_age_seconds)
        return result[mint]

    async def get_many(self, mints: Sequence[str], *, max_age_seconds: float | None = None) -> dict[str, TokenInfo]:
        mints = list(dict.fromkeys(mints))
        now_mono = self.clock.monotonic()
        max_age = max_age_seconds if max_age_seconds is not None else self._config().providers.token_info_ttl_seconds
        stale = [m for m in mints if not (c := self._market_cache.get(m, now_mono)) or now_mono - c[1] > max_age]
        if stale:
            try:
                fresh = await self.market.token_market(stale)
            except CopyTraderError as exc:
                log.warning("market_data_failed", error=str(exc), n=len(stale))
                fresh = {}
            wall = self.clock.now()
            for mint, data in fresh.items():
                self._market_cache.set(mint, (data, now_mono, wall), now_mono)
        statics = await asyncio.gather(*(self._static(m) for m in mints))
        out: dict[str, TokenInfo] = {}
        for mint, (mint_data, risk_data) in zip(mints, statics, strict=True):
            cached = self._market_cache.get(mint, now_mono)
            info = self._merge(
                mint, cached[0] if cached else None, mint_data, risk_data, market_at=cached[2] if cached else None
            )
            out[mint] = info
            if self._on_update is not None:
                try:
                    self._on_update(info)
                except Exception:
                    log.exception("token_update_hook_failed")
        return out

    async def _static(self, mint: str) -> tuple[MintData | None, RiskData | None]:
        cached = self._static_cache.get(mint)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(mint, asyncio.Lock())
        async with lock:
            cached = self._static_cache.get(mint)
            if cached is not None:
                return cached
            mint_task = self._safe(self.mint_source.mint_info(mint)) if self.mint_source else _none()
            risk_task = self._safe(self.risk.token_risk(mint)) if self.risk else _none()
            mint_data, risk_data = await asyncio.gather(mint_task, risk_task)
            # Only cache complete results; retry sooner when a provider failed.
            ttl = None if (mint_data is not None or self.mint_source is None) else 30.0
            self._static_cache.set(mint, (mint_data, risk_data), ttl=ttl)
            self._locks.pop(mint, None)
            return mint_data, risk_data

    @staticmethod
    async def _safe(coro: Awaitable[_T]) -> _T | None:
        try:
            return await coro
        except CopyTraderError as exc:
            log.warning("token_static_source_failed", error=str(exc))
            return None

    def _merge(
        self,
        mint: str,
        market: MarketData | None,
        mint_data: MintData | None,
        risk: RiskData | None,
        *,
        market_at: datetime | None,
    ) -> TokenInfo:
        # ``fetched_at`` is the age of the *market* data: freshness checks rely on it.
        info = TokenInfo(mint=mint, fetched_at=market_at or self.clock.now())
        sources: list[str] = []
        if market is not None:
            sources.append("market")
            info.symbol, info.name = market.symbol, market.name
            info.price_usd, info.liquidity_usd = market.price_usd, market.liquidity_usd
            info.market_cap_usd, info.fdv_usd = market.market_cap_usd, market.fdv_usd
            info.volume_24h_usd = market.volume_24h_usd
            info.pair_created_at = market.pair_created_at
            info.price_change_pct = dict(market.price_change_pct)
            info.dex, info.pair_address = market.dex, market.pair_address
        if mint_data is not None:
            sources.append("mint")
            info.decimals = mint_data.decimals
            info.token_program = mint_data.token_program
            info.mint_authority = mint_data.mint_authority
            info.freeze_authority = mint_data.freeze_authority
            info.dangerous_extensions = list(mint_data.dangerous_extensions)
        if risk is not None:
            sources.append("risk")
            info.risk_score, info.risk_level = risk.score, risk.level
            info.risk_flags = list(risk.flags)
            info.is_rugged = risk.is_rugged
        info.sources = sources
        info.category = self.categorizer.categorize(mint, info.market_cap_usd, info.dex)
        return info


async def _none() -> None:
    return None
