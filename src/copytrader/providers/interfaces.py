"""Blockchain Data Layer contracts.

Everything above this layer (analysis, scoring, pipeline, risk, execution)
depends only on these protocols. Solana adapters live in ``providers/solana``
and friends; the simulated market implements the same contracts, and an EVM
adapter would too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from copytrader.core.models import Quote, SwapEvent, TokenInfo
from copytrader.core.types import TxSource
from copytrader.resilience.rate_limiter import Priority

SwapHandler = Callable[[SwapEvent], Awaitable[None]]


class SwapFeed(Protocol):
    """Real-time stream of parsed swaps for a dynamic set of wallets."""

    async def run(self, on_swap: SwapHandler) -> None: ...

    def set_wallets(self, wallets: set[str]) -> None: ...

    async def stop(self) -> None: ...


class HistorySource(Protocol):
    # Transactions of the last fetch of each wallet that could not be downloaded (0 = complete).
    missing: dict[str, int]

    async def fetch_swaps(
        self,
        wallet: str,
        *,
        since: datetime | None = None,
        until_signature: str | None = None,
        max_signatures: int = 1000,
        source: TxSource = TxSource.BACKFILL,
        skip: Collection[str] = (),
    ) -> list[SwapEvent]: ...


@dataclass(slots=True)
class MarketData:
    mint: str
    symbol: str | None = None
    name: str | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    market_cap_usd: float | None = None
    fdv_usd: float | None = None
    volume_24h_usd: float | None = None
    pair_created_at: datetime | None = None
    price_change_pct: dict[str, float] = field(default_factory=dict)
    dex: str | None = None
    pair_address: str | None = None


@dataclass(slots=True)
class RiskData:
    mint: str
    score: float | None = None  # normalised 0 (safe) .. 100 (danger)
    level: str | None = None
    flags: list[str] = field(default_factory=list)
    is_rugged: bool = False


@dataclass(slots=True)
class MintData:
    mint: str
    decimals: int | None = None
    token_program: str | None = None
    mint_authority: str | None = None
    freeze_authority: str | None = None
    dangerous_extensions: list[str] = field(default_factory=list)


class MarketDataSource(Protocol):
    async def token_market(self, mints: Sequence[str]) -> dict[str, MarketData]: ...


class TokenRiskSource(Protocol):
    async def token_risk(self, mint: str) -> RiskData | None: ...


class MintInfoSource(Protocol):
    async def mint_info(self, mint: str) -> MintData | None: ...


class PriceSource(Protocol):
    async def prices_usd(self, mints: Sequence[str]) -> dict[str, float]: ...


class SolPriceHistory(Protocol):
    async def sol_price_at(self, ts: datetime) -> float | None: ...

    async def sol_series(self, start: datetime, end: datetime) -> list[tuple[datetime, float]]: ...


class QuoteSource(Protocol):
    async def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        slippage_bps: int,
        *,
        priority: int = Priority.EXECUTION,
    ) -> Quote: ...


@dataclass(frozen=True, slots=True)
class BuiltTransaction:
    tx_bytes: bytes
    last_valid_block_height: int
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


class SwapTxBuilder(Protocol):
    async def build_swap(
        self,
        quote: Quote,
        user_public_key: str,
        *,
        priority_max_lamports: int,
        priority_level: str,
        jito_tip_lamports: int = 0,
    ) -> BuiltTransaction: ...


class ChainClient(Protocol):
    async def send_raw_transaction(self, tx_bytes: bytes, *, skip_preflight: bool = True) -> str: ...

    async def get_signature_statuses(self, signatures: Sequence[str]) -> list[dict[str, Any] | None]: ...

    async def get_block_height(self) -> int: ...

    async def get_transaction(self, signature: str, *, commitment: str | None = None) -> dict[str, Any] | None: ...

    async def get_balance(self, address: str) -> int: ...

    async def get_token_balances(self, owner: str) -> dict[str, int]: ...

    async def get_health(self) -> bool: ...


class TokenInfoProvider(Protocol):
    """What the pipeline/risk engine consume: a merged, cached ``TokenInfo``."""

    async def get(self, mint: str, *, max_age_seconds: float | None = None) -> TokenInfo: ...

    async def get_many(self, mints: Sequence[str], *, max_age_seconds: float | None = None) -> dict[str, TokenInfo]: ...

    async def prices(self, mints: Sequence[str], *, max_age_seconds: float | None = None) -> dict[str, float]: ...

    async def sol_price(self) -> float | None: ...
