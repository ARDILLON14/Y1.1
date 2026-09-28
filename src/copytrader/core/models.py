"""Domain value objects shared between components.

These are plain dataclasses with no I/O so they can be created freely in tests
and passed across layer boundaries without leaking ORM or HTTP details.

Conventions:
* All datetimes are timezone-aware UTC.
* ``*_pct`` fields are percentages (5.0 == 5 %), ``*_frac`` / ``return_*``
  fields are fractions (0.05 == 5 %).
* ``*_raw`` amounts are integers in the token's base units (lamports, etc.).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from copytrader.core.types import (
    OrderPurpose,
    Severity,
    Side,
    TradeMode,
    TxSource,
)


@dataclass(frozen=True, slots=True)
class SwapEvent:
    """A single swap performed by a tracked wallet, parsed from chain data."""

    wallet: str
    signature: str
    slot: int
    block_time: datetime
    token_mint: str
    side: Side
    token_amount: float
    token_decimals: int
    quote_mint: str
    quote_amount: float
    price_quote: float
    price_usd: float | None
    value_usd: float | None
    sol_price_usd: float | None = None
    fee_sol: float = 0.0
    dex: str = "unknown"
    token_balance_before: float | None = None
    token_balance_after: float | None = None
    source: TxSource = TxSource.STREAM
    detected_at: datetime | None = None
    liquidity_usd: float | None = None

    @property
    def sold_fraction(self) -> float | None:
        """Fraction of the wallet's holding sold in this swap (sells only)."""
        if self.side is not Side.SELL:
            return None
        before = self.token_balance_before
        if before is None or before <= 0:
            return None
        sold = before - (self.token_balance_after or 0.0)
        return max(0.0, min(1.0, sold / before))

    @property
    def detection_latency_ms(self) -> float | None:
        if self.detected_at is None:
            return None
        return (self.detected_at - self.block_time).total_seconds() * 1000.0


# minutes covered by each price-change window (DexScreener keys)
PRICE_CHANGE_WINDOWS = {"m5": 5, "h1": 60, "h6": 360, "h24": 1440}


def hourly_volatility_from_changes(price_change_pct: dict[str, float] | None) -> float | None:
    """Crude hourly volatility estimate (fraction) from price-change windows (in %).

    Uses the largest of |m5|·√12, |h1|, |h6|/√6 and |h24|/√24 so that a
    token that is moving violently right now is not diluted by a calm day.
    """
    if not price_change_pct:
        return None
    candidates: list[float] = []
    for key, minutes in PRICE_CHANGE_WINDOWS.items():
        value = price_change_pct.get(key)
        if value is not None:
            candidates.append(abs(value) / 100.0 * math.sqrt(60 / minutes))
    return max(candidates) if candidates else None


@dataclass(slots=True)
class TokenInfo:
    """Market + risk snapshot of a token, merged from several providers."""

    mint: str
    fetched_at: datetime
    symbol: str | None = None
    name: str | None = None
    decimals: int | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    market_cap_usd: float | None = None
    fdv_usd: float | None = None
    volume_24h_usd: float | None = None
    pair_created_at: datetime | None = None
    price_change_pct: dict[str, float] = field(default_factory=dict)
    dex: str | None = None
    pair_address: str | None = None
    token_program: str | None = None
    mint_authority: str | None = None
    freeze_authority: str | None = None
    risk_score: float | None = None  # 0 = safe ... 100 = extremely risky
    risk_level: str | None = None  # low | medium | high | critical
    risk_flags: list[str] = field(default_factory=list)
    dangerous_extensions: list[str] = field(default_factory=list)
    is_rugged: bool = False
    category: str = "other"
    sources: list[str] = field(default_factory=list)

    def age_minutes(self, now: datetime) -> float | None:
        if self.pair_created_at is None:
            return None
        return max(0.0, (now - self.pair_created_at).total_seconds() / 60.0)

    def hourly_volatility(self) -> float | None:
        return hourly_volatility_from_changes(self.price_change_pct)

    def age_seconds_of_data(self, now: datetime) -> float:
        return (now - self.fetched_at).total_seconds()


@dataclass(frozen=True, slots=True)
class Quote:
    """A firm-ish swap quote for a specific size."""

    input_mint: str
    output_mint: str
    in_amount_raw: int
    out_amount_raw: int
    min_out_amount_raw: int
    slippage_bps: int
    price_impact_frac: float
    obtained_at: datetime
    route_label: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of one validation step. Rendered verbatim in explanations."""

    name: str
    label: str
    passed: bool
    value: Any = None
    limit: Any = None
    message: str = ""
    critical: bool = True

    def render(self) -> str:
        mark = "✓" if self.passed else ("✗" if self.critical else "!")
        text = f"{mark} {self.label}"
        if self.message:
            text += f" — {self.message}"
        return text


@dataclass(slots=True)
class Decision:
    """Result of the copy pipeline for one signal."""

    approved: bool
    checks: list[CheckResult] = field(default_factory=list)
    reason: str | None = None
    size_usd: float | None = None
    sizing: dict[str, Any] = field(default_factory=dict)
    prices: dict[str, float | None] = field(default_factory=dict)

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and c.critical]

    def explain(self, header: str = "") -> str:
        lines: list[str] = []
        if header:
            lines.append(header)
            lines.append("")
        lines.extend(c.render() for c in self.checks)
        lines.append("")
        lines.append(f"EXECUTION: {'APPROVED' if self.approved else 'REJECTED'}")
        if not self.approved and self.reason:
            lines.append("")
            lines.append("Reason:")
            lines.append(self.reason)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "size_usd": self.size_usd,
            "sizing": self.sizing,
            "prices": self.prices,
            "checks": [
                {
                    "name": c.name,
                    "label": c.label,
                    "passed": c.passed,
                    "value": _jsonable(c.value),
                    "limit": _jsonable(c.limit),
                    "message": c.message,
                    "critical": c.critical,
                }
                for c in self.checks
            ],
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, 8)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True, slots=True)
class Flag:
    """Suspicious-behaviour finding about a wallet."""

    code: str
    severity: Severity
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ClosedTrade:
    """A completed round trip (flat → position → flat) for one token."""

    wallet: str
    token_mint: str
    opened_at: datetime
    closed_at: datetime
    cost_usd: float
    proceeds_usd: float
    pnl_usd: float
    return_frac: float
    holding_seconds: float
    n_buys: int
    n_sells: int
    entry_price_usd: float
    category: str | None = None
    liquidity_at_entry_usd: float | None = None
    regime: str | None = None
    entry_value_usd: float | None = None  # value of the FIRST buy (the one a copier follows)
    exit_price_usd: float | None = None  # average price of every sell in the round trip

    @property
    def is_win(self) -> bool:
        return self.pnl_usd > 0


@dataclass(slots=True)
class OpenLot:
    """A position still open at the end of the analysed history."""

    wallet: str
    token_mint: str
    qty: float
    cost_usd: float
    opened_at: datetime
    last_trade_at: datetime
    mark_price_usd: float | None = None
    stale: bool = False

    @property
    def avg_price_usd(self) -> float:
        return self.cost_usd / self.qty if self.qty > 0 else 0.0

    @property
    def unrealized_pnl_usd(self) -> float | None:
        if self.mark_price_usd is None or self.stale:
            return None
        return self.qty * self.mark_price_usd - self.cost_usd


@dataclass(slots=True)
class TradeIntent:
    """What the pipeline wants to do, expressed for the risk engine."""

    token_mint: str
    side: Side
    size_usd: float
    mode: TradeMode
    source_wallet: str | None = None
    wallet_score: float | None = None
    is_high_risk: bool = False
    category: str | None = None
    liquidity_usd: float | None = None
    est_slippage_bps: float | None = None


@dataclass(slots=True)
class OrderRequest:
    """Instruction for an executor. Amounts are raw base units."""

    client_order_id: str
    purpose: OrderPurpose
    side: Side
    mode: TradeMode
    token_mint: str
    token_decimals: int
    input_mint: str
    output_mint: str
    amount_in_raw: int
    slippage_bps: int
    signal_id: int | None = None
    position_id: int | None = None
    signal_price_usd: float | None = None
    theoretical_price_usd: float | None = None
    notional_usd: float | None = None
    max_price_deviation_pct: float | None = None
    expires_at: datetime | None = None
    trace_id: str | None = None
    trigger: str | None = None  # exits: what triggered it (decides the fee urgency)
    attempt: int = 1  # exits: 2+ when a previous exit of the position failed


@dataclass(slots=True)
class ExecutionResult:
    """What actually happened when an order was executed (or simulated)."""

    success: bool
    client_order_id: str
    mode: TradeMode
    tx_signature: str | None = None
    in_amount_raw: int = 0
    out_amount_raw: int = 0
    token_qty: float = 0.0
    fill_price_usd: float | None = None
    value_usd: float = 0.0
    fees_usd: float = 0.0
    quote_price_usd: float | None = None
    slippage_bps: float | None = None
    price_impact_bps: float | None = None
    latency_ms: float | None = None
    error: str | None = None
    executed_at: datetime | None = None
    retryable: bool = False
    network_fee_lamports: int | None = None  # base + priority fee + tip (paid live; modeled in paper)
    fee_decision: dict[str, Any] | None = None  # what the fee policy chose for this transaction
