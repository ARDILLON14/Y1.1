"""ORM tables. See docs/ARCHITECTURE.md §6 for the rationale of each table.

UNIQUE constraints marked "idempotency" are the last line of defence against
duplicate processing: even if a bug or a restart re-processes an event, the
database rejects the second insert.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from copytrader.core.clock import utcnow
from copytrader.db.base import Base, JSONType, RawAmount


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    chain: Mapped[str] = mapped_column(String(16), default="solana")
    label: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    list_type: Mapped[str] = mapped_column(String(16), default="none", index=True)
    status: Mapped[str] = mapped_column(String(16), default="observe", index=True)
    status_reasons: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    status_changed_at: Mapped[datetime | None]
    exit_mode_override: Mapped[str | None] = mapped_column(String(16))
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    score: Mapped[float | None] = mapped_column(Float)
    rank: Mapped[int | None] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_activity_at: Mapped[datetime | None]
    last_seen_signature: Mapped[str | None] = mapped_column(String(100))
    last_seen_slot: Mapped[int | None] = mapped_column(BigInteger)
    backfilled_at: Mapped[datetime | None]
    analyzed_at: Mapped[datetime | None]


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    mint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(40))
    name: Mapped[str | None] = mapped_column(String(120))
    decimals: Mapped[int | None] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(40), default="other")
    token_program: Mapped[str | None] = mapped_column(String(64))
    mint_authority: Mapped[str | None] = mapped_column(String(64))
    freeze_authority: Mapped[str | None] = mapped_column(String(64))
    risk_score: Mapped[float | None] = mapped_column(Float)
    risk_level: Mapped[str | None] = mapped_column(String(16))
    risk_flags: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    is_rugged: Mapped[bool] = mapped_column(Boolean, default=False)
    pair_created_at: Mapped[datetime | None]
    last_price_usd: Mapped[float | None] = mapped_column(Float)
    last_liquidity_usd: Mapped[float | None] = mapped_column(Float)
    last_market_cap_usd: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class TokenSnapshot(Base):
    __tablename__ = "token_snapshots"
    __table_args__ = (Index("ix_token_snapshots_mint_ts", "mint", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mint: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    price_usd: Mapped[float | None] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float)
    market_cap_usd: Mapped[float | None] = mapped_column(Float)
    volume_24h_usd: Mapped[float | None] = mapped_column(Float)


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    __table_args__ = (
        # idempotency: one row per (wallet, tx, token, side)
        UniqueConstraint("wallet_id", "signature", "token_mint", "side", name="uq_wallet_tx"),
        Index("ix_wallet_tx_wallet_time", "wallet_id", "block_time"),
        Index("ix_wallet_tx_token_time", "token_mint", "block_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"))
    signature: Mapped[str] = mapped_column(String(100))
    slot: Mapped[int] = mapped_column(BigInteger)
    block_time: Mapped[datetime]
    token_mint: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))
    token_amount: Mapped[float] = mapped_column(Float)
    token_decimals: Mapped[int] = mapped_column(Integer)
    quote_mint: Mapped[str] = mapped_column(String(64))
    quote_amount: Mapped[float] = mapped_column(Float)
    price_quote: Mapped[float] = mapped_column(Float)
    price_usd: Mapped[float | None] = mapped_column(Float)
    value_usd: Mapped[float | None] = mapped_column(Float)
    sol_price_usd: Mapped[float | None] = mapped_column(Float)
    fee_sol: Mapped[float] = mapped_column(Float, default=0.0)
    dex: Mapped[str] = mapped_column(String(40), default="unknown")
    token_balance_before: Mapped[float | None] = mapped_column(Float)
    token_balance_after: Mapped[float | None] = mapped_column(Float)
    liquidity_usd_at_trade: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))
    detected_at: Mapped[datetime | None]
    detection_latency_ms: Mapped[float | None] = mapped_column(Float)


class WalletMetric(Base):
    __tablename__ = "wallet_metrics"
    __table_args__ = (Index("ix_wallet_metrics_wallet_time", "wallet_id", "computed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"))
    computed_at: Mapped[datetime] = mapped_column(default=utcnow)
    window: Mapped[str] = mapped_column(String(16))  # all | recent | decayed
    n_trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float | None] = mapped_column(Float)
    profit_factor: Mapped[float | None] = mapped_column(Float)
    roi_pct: Mapped[float | None] = mapped_column(Float)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float)
    realized_pnl_usd: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl_usd: Mapped[float | None] = mapped_column(Float)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class WalletScore(Base):
    __tablename__ = "wallet_scores"
    __table_args__ = (Index("ix_wallet_scores_wallet_time", "wallet_id", "computed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"))
    computed_at: Mapped[datetime] = mapped_column(default=utcnow)
    score: Mapped[float] = mapped_column(Float)
    score_hist: Mapped[float | None] = mapped_column(Float)
    score_recent: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    components: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    penalties: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(16))
    status_reasons: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    rank: Mapped[int | None] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)


class WalletFlag(Base):
    __tablename__ = "wallet_flags"
    __table_args__ = (UniqueConstraint("wallet_id", "code", name="uq_wallet_flag"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)


class SelectionSnapshot(Base):
    __tablename__ = "selection_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    top_n: Mapped[int] = mapped_column(Integer)
    selected: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    config_version: Mapped[int] = mapped_column(Integer, default=0)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_key: Mapped[str] = mapped_column(String(64), unique=True)  # idempotency
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"), index=True)
    source_signature: Mapped[str] = mapped_column(String(100))
    token_mint: Mapped[str] = mapped_column(String(64), index=True)
    token_symbol: Mapped[str | None] = mapped_column(String(40))
    side: Mapped[str] = mapped_column(String(8))
    action: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    source_price_usd: Mapped[float | None] = mapped_column(Float)
    source_value_usd: Mapped[float | None] = mapped_column(Float)
    source_block_time: Mapped[datetime]
    detected_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    detection_latency_ms: Mapped[float | None] = mapped_column(Float)
    wallet_score: Mapped[float | None] = mapped_column(Float)
    operating_level: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str | None] = mapped_column(String(8))
    reason: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)  # idempotency
    trace_id: Mapped[str | None] = mapped_column(String(32))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), index=True)
    mode: Mapped[str] = mapped_column(String(8))
    purpose: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    token_mint: Mapped[str] = mapped_column(String(64))
    input_mint: Mapped[str] = mapped_column(String(64))
    output_mint: Mapped[str] = mapped_column(String(64))
    amount_in_raw: Mapped[int] = mapped_column(RawAmount)
    expected_out_raw: Mapped[int | None] = mapped_column(RawAmount)
    min_out_raw: Mapped[int | None] = mapped_column(RawAmount)
    notional_usd: Mapped[float | None] = mapped_column(Float)
    slippage_bps: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True)
    tx_signature: Mapped[str | None] = mapped_column(String(100), unique=True)
    last_valid_block_height: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    trigger: Mapped[str | None] = mapped_column(String(40))
    context: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)  # idempotency
    mode: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    token_mint: Mapped[str] = mapped_column(String(64), index=True)
    tx_signature: Mapped[str | None] = mapped_column(String(100))
    in_amount_raw: Mapped[int] = mapped_column(RawAmount)
    out_amount_raw: Mapped[int] = mapped_column(RawAmount)
    token_qty: Mapped[float] = mapped_column(Float)
    signal_price_usd: Mapped[float | None] = mapped_column(Float)
    theoretical_price_usd: Mapped[float | None] = mapped_column(Float)
    quote_price_usd: Mapped[float | None] = mapped_column(Float)
    fill_price_usd: Mapped[float | None] = mapped_column(Float)
    slippage_bps: Mapped[float | None] = mapped_column(Float)
    price_impact_bps: Mapped[float | None] = mapped_column(Float)
    value_usd: Mapped[float] = mapped_column(Float)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl_usd: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    executed_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_mode_status", "mode", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    token_mint: Mapped[str] = mapped_column(String(64), index=True)
    token_symbol: Mapped[str | None] = mapped_column(String(40))
    decimals: Mapped[int] = mapped_column(Integer)
    source_wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id"), index=True)
    entry_signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    exit_mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), index=True)
    qty_raw: Mapped[int] = mapped_column(RawAmount)
    initial_qty_raw: Mapped[int] = mapped_column(RawAmount)
    cost_usd: Mapped[float] = mapped_column(Float)  # remaining cost basis
    initial_cost_usd: Mapped[float] = mapped_column(Float)
    entry_price_usd: Mapped[float] = mapped_column(Float)
    peak_price_usd: Mapped[float] = mapped_column(Float)
    last_price_usd: Mapped[float | None] = mapped_column(Float)
    last_price_at: Mapped[datetime | None]
    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tp_levels_hit: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    exit_params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    exit_seq: Mapped[int] = mapped_column(Integer, default=0)
    is_high_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str | None] = mapped_column(String(40))
    at_risk_usd: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(default=utcnow)
    closed_at: Mapped[datetime | None]
    close_reason: Mapped[str | None] = mapped_column(Text)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_equity_mode_ts", "mode", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    mode: Mapped[str] = mapped_column(String(8))
    equity_usd: Mapped[float] = mapped_column(Float)
    cash_usd: Mapped[float] = mapped_column(Float)
    exposure_usd: Mapped[float] = mapped_column(Float)
    realized_pnl_usd: Mapped[float] = mapped_column(Float)
    unrealized_pnl_usd: Mapped[float] = mapped_column(Float)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    type: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(120))
    channels: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


class ConfigVersionRow(Base):
    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    author: Mapped[str] = mapped_column(String(64))
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(200))
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16))
    component: Mapped[str] = mapped_column(String(40))
    event: Mapped[str] = mapped_column(String(80))
    trace_id: Mapped[str | None] = mapped_column(String(32), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    totp_secret_enc: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    password_changed_at: Mapped[datetime] = mapped_column(default=utcnow)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    results: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class SignalOutcome(Base):
    """What the token did after a COPY decision — for executed AND rejected signals.

    Comparing the forward returns of rejected signals (grouped by the check that
    rejected them) with those of executed ones shows which filters protect the
    capital and which ones only cost opportunities.
    """

    __tablename__ = "signal_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"), unique=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"), index=True)
    token_mint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    mode: Mapped[str | None] = mapped_column(String(8))
    failed_check: Mapped[str | None] = mapped_column(String(40), index=True)
    failed_label: Mapped[str | None] = mapped_column(String(120))
    reference_price_usd: Mapped[float] = mapped_column(Float)
    reference_at: Mapped[datetime] = mapped_column(index=True)
    # {"5": 0.12, "60": -0.03, "1440": null}: return vs reference per horizon (minutes); null = missed
    returns: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class PriceCandle(Base):
    """Historical OHLCV candle of a token (USD), cached for backtesting."""

    __tablename__ = "price_candles"
    __table_args__ = (
        UniqueConstraint("mint", "interval_minutes", "ts", name="uq_price_candle"),  # idempotency
        Index("ix_price_candles_mint_ts", "mint", "interval_minutes", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mint: Mapped[str] = mapped_column(String(64))
    interval_minutes: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime]  # start of the candle
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume_usd: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(24))


class PriceFetch(Base):
    """A download attempt of a token's candles for a time range (the empty and failed ones too,
    so a token without history is not asked for again on every backtest)."""

    __tablename__ = "price_fetches"
    __table_args__ = (Index("ix_price_fetches_mint", "mint", "interval_minutes"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mint: Mapped[str] = mapped_column(String(64))
    interval_minutes: Mapped[int] = mapped_column(Integer)
    start: Mapped[datetime]
    end: Mapped[datetime]
    source: Mapped[str] = mapped_column(String(24))
    pool: Mapped[str | None] = mapped_column(String(64))
    candles: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)
