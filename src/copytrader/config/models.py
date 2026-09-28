"""Central configuration schema.

Every tunable of the system lives here, validated by Pydantic. Components read
``ConfigService.current`` at the moment they need a value, so runtime changes
(made from the dashboard and versioned in the DB) take effect without restarts.

Units: ``*_pct`` are percentages (5 == 5 %), ``*_usd`` US dollars, ``*_bps``
basis points, ``*_seconds``/``*_minutes`` durations.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from copytrader.config import hard_limits as HL
from copytrader.core.types import AlertType, ExitMode, OperatingLevel, Severity

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=False)


# --------------------------------------------------------------------------- app
class AppSection(Section):
    name: str = "copytrader"
    environment: Literal["development", "production", "test"] = "production"
    operating_level: OperatingLevel = OperatingLevel.ANALYSIS
    chain: Literal["solana"] = "solana"
    data_dir: str = "data"


class WalletsSection(Section):
    max_wallets: int = Field(100, ge=1, le=1000)
    import_file: str | None = None


# ---------------------------------------------------------------------- providers
class RetrySection(Section):
    max_attempts: int = Field(3, ge=1, le=10)
    base_delay_seconds: float = Field(0.2, gt=0)
    max_delay_seconds: float = Field(3.0, gt=0)


class CircuitBreakerSection(Section):
    failure_threshold: int = Field(5, ge=1)
    reset_timeout_seconds: float = Field(30.0, gt=0)


class SolanaProviderSection(Section):
    rpc_http_url: str = "https://api.mainnet-beta.solana.com"
    rpc_ws_url: str = "wss://api.mainnet-beta.solana.com"
    stream: Literal["logs_subscribe", "helius_transaction_subscribe"] = "logs_subscribe"
    commitment: Literal["processed", "confirmed", "finalized"] = "confirmed"
    max_subscriptions_per_connection: int = Field(100, ge=1, le=1000)
    ws_ping_interval_seconds: float = Field(20.0, gt=0)
    ws_stale_timeout_seconds: float = Field(180.0, gt=0)
    reconnect_max_backoff_seconds: float = Field(60.0, gt=0)
    get_transaction_retries: int = Field(8, ge=1, le=30)
    get_transaction_retry_delay_ms: int = Field(250, ge=50)
    catchup_on_reconnect: bool = True
    reconcile_poll_interval_seconds: int = Field(300, ge=30)
    backfill_max_signatures_per_wallet: int = Field(1000, ge=10, le=100_000)
    backfill_concurrency: int = Field(4, ge=1, le=64)
    rate_limit_per_second: float = Field(20.0, gt=0)
    timeout_seconds: float = Field(10.0, gt=0)


class JupiterSection(Section):
    quote_url: str = "https://lite-api.jup.ag/swap/v1/quote"
    swap_url: str = "https://lite-api.jup.ag/swap/v1/swap"
    price_url: str = "https://lite-api.jup.ag/price/v3"
    rate_limit_per_second: float = Field(1.0, gt=0)
    timeout_seconds: float = Field(5.0, gt=0)
    restrict_intermediate_tokens: bool = True


class DexScreenerSection(Section):
    base_url: str = "https://api.dexscreener.com"
    rate_limit_per_second: float = Field(4.0, gt=0)
    timeout_seconds: float = Field(5.0, gt=0)


class RugCheckSection(Section):
    enabled: bool = True
    base_url: str = "https://api.rugcheck.xyz/v1"
    rate_limit_per_second: float = Field(2.0, gt=0)
    timeout_seconds: float = Field(5.0, gt=0)


class SimulatedSection(Section):
    seed: int = 7
    n_wallets: int = Field(40, ge=1, le=500)
    n_tokens: int = Field(60, ge=5, le=2000)
    history_days: int = Field(60, ge=1, le=365)
    realtime_trades_per_minute: float = Field(6.0, ge=0)
    speedup: float = Field(1.0, gt=0)


class ProvidersSection(Section):
    mode: Literal["live", "simulated"] = "simulated"
    solana: SolanaProviderSection = SolanaProviderSection()
    jupiter: JupiterSection = JupiterSection()
    dexscreener: DexScreenerSection = DexScreenerSection()
    rugcheck: RugCheckSection = RugCheckSection()
    simulated: SimulatedSection = SimulatedSection()
    retry: RetrySection = RetrySection()
    circuit_breaker: CircuitBreakerSection = CircuitBreakerSection()
    token_info_ttl_seconds: float = Field(20.0, gt=0)
    token_static_ttl_seconds: float = Field(3600.0, gt=0)
    price_ttl_seconds: float = Field(3.0, gt=0)
    quote_mints: list[str] = Field(default_factory=lambda: [SOL_MINT, USDC_MINT, USDT_MINT])
    sol_price_history_url: str = "https://data-api.binance.vision/api/v3/klines"
    sol_price_symbol: str = "SOLUSDT"
    token_categories_file: str | None = "config/token_categories.yaml"


# ----------------------------------------------------------------------- analysis
class AnalysisSection(Section):
    recompute_interval_seconds: int = Field(900, ge=30)
    history_days: int = Field(180, ge=1)
    recent_trades: int = Field(30, ge=5)
    decay_half_life_days: float = Field(45.0, gt=0)
    min_trade_usd: float = Field(5.0, ge=0)
    dust_fraction: float = Field(0.01, gt=0, lt=0.5)
    stale_position_days: float = Field(30.0, gt=0)
    fast_trade_max_minutes: float = Field(60.0, gt=0)
    holding_buckets_minutes: list[float] = Field(default_factory=lambda: [5.0, 60.0, 1440.0])
    min_replicable_hold_seconds: float = Field(60.0, ge=0)
    regime_trend_threshold_pct: float = Field(3.0, gt=0)
    regime_high_vol_threshold_pct: float = Field(6.0, gt=0)
    metrics_snapshot_hours: float = Field(24.0, gt=0, description="new metrics row at most every N hours")
    # Copy replication: estimate what copying each trade would have returned (analysis/replication.py).
    replication_enabled: bool = True
    # None = median latency measured on our own recent copies (≥ replication_min_latency_samples),
    # falling back to backtest.latency_seconds.
    replication_latency_seconds: float | None = Field(None, ge=0)
    replication_min_latency_samples: int = Field(20, ge=1)
    # None = the size the risk-based sizing would use (capital × risk per trade / stop loss, capped).
    replication_size_usd: float | None = Field(None, gt=0)
    score_snapshot_minutes: float = Field(60.0, gt=0, description="new score row at most every N minutes")


# ------------------------------------------------------------------------ scoring
class ScoringWeights(Section):
    profitability: float = Field(0.18, ge=0)
    consistency: float = Field(0.14, ge=0)
    drawdown: float = Field(0.12, ge=0)
    win_rate: float = Field(0.10, ge=0)
    profit_factor: float = Field(0.10, ge=0)
    risk: float = Field(0.07, ge=0)
    volatility: float = Field(0.05, ge=0)
    sample_size: float = Field(0.06, ge=0)
    activity: float = Field(0.05, ge=0)
    concentration: float = Field(0.06, ge=0)
    extreme_moves: float = Field(0.03, ge=0)
    replicability: float = Field(0.04, ge=0)
    copy_edge: float = Field(0.18, ge=0)  # estimated return of COPYING the wallet (latency, impact, costs)

    @model_validator(mode="after")
    def _non_zero(self) -> ScoringWeights:
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("at least one scoring weight must be > 0")
        return self


class ScoringBounds(Section):
    """Normalisation ranges: value at ``lo`` → 0, at ``hi`` → 1 (clipped)."""

    expectancy_lo_pct: float = -5.0
    expectancy_hi_pct: float = 25.0
    # Copied returns are structurally lower than the wallet's own (latency, impact, costs).
    copy_expectancy_lo_pct: float = -5.0
    copy_expectancy_hi_pct: float = 15.0
    roi_lo_pct: float = -20.0
    roi_hi_pct: float = 100.0
    win_rate_lo: float = 0.30
    win_rate_hi: float = 0.70
    profit_factor_hi: float = 3.0
    drawdown_max_pct: float = 60.0
    return_std_hi_pct: float = 150.0
    avg_loss_hi_pct: float = 50.0
    worst_loss_hi_pct: float = 95.0
    trades_per_day_max: float = 40.0
    inactive_days_zero: float = 30.0
    profitable_weeks_hi: float = 0.75


class SampleSection(Section):
    prior_trades: float = Field(30.0, gt=0, description="k in n/(n+k) shrinkage")
    prior_score: float = Field(40.0, ge=0, le=100)
    confidence_z: float = Field(1.645, gt=0)
    profit_factor_prior_trades: float = Field(10.0, ge=0)


class DegradationSection(Section):
    min_recent_trades: int = Field(15, ge=5)
    win_rate_drop: float = Field(0.15, gt=0, lt=1)
    p_value: float = Field(0.05, gt=0, lt=1)
    recent_profit_factor_floor: float = Field(1.0, ge=0)
    historical_profit_factor_min: float = Field(1.3, ge=0)
    penalty_points: float = Field(10.0, ge=0)


class ScoringSection(Section):
    weights: ScoringWeights = ScoringWeights()
    bounds: ScoringBounds = ScoringBounds()
    sample: SampleSection = SampleSection()
    degradation: DegradationSection = DegradationSection()
    recent_weight: float = Field(0.4, ge=0, le=1)
    warning_penalty_points: float = Field(5.0, ge=0)
    max_warning_penalty_points: float = Field(25.0, ge=0)


class StatusRulesSection(Section):
    min_score_active: float = Field(55.0, ge=0, le=100)
    min_trades_active: int = Field(25, ge=1)
    max_inactive_days: float = Field(14.0, gt=0)
    observe_on_degradation: bool = True
    block_on_critical_flag: bool = True
    block_below_score: float | None = Field(None, ge=0, le=100)
    # OBSERVE a wallet whose estimated copied return per trade is below this (%). None disables.
    min_copy_expectancy_pct: float | None = 0.0


# ---------------------------------------------------------------------- detection
class DetectionSection(Section):
    wash_max_abs_return_pct: float = 1.0
    wash_max_hold_minutes: float = 10.0
    wash_min_roundtrips: int = 5
    wash_fraction_warning: float = 0.25
    wash_fraction_critical: float = 0.5
    coordination_window_seconds: float = 20.0
    coordination_min_other_wallets: int = 2
    coordination_fraction: float = 0.35
    sniper_window_seconds: float = 120.0
    sniper_fraction: float = 0.4
    low_liquidity_usd: float = 20_000.0
    low_liquidity_fraction: float = 0.4
    unreplicable_hold_seconds: float = 60.0
    unreplicable_fraction: float = 0.4
    single_trade_share_warning: float = 0.5
    single_trade_share_critical: float = 0.85
    outlier_return_multiple: float = 10.0
    outlier_pnl_share: float = 0.6
    rug_fraction: float = 0.2
    high_risk_fraction: float = 0.4
    high_risk_token_score: float = 60.0
    behavior_change_ratio: float = 3.0
    behavior_min_recent: int = 15
    hft_trades_per_day: float = 150.0
    inactive_days: float = 14.0
    copy_impact_liquidity_fraction: float = 0.02
    copy_impact_fraction: float = 0.3
    min_trades_for_detection: int = 5
    severity_overrides: dict[str, Severity] = Field(default_factory=dict)


# ---------------------------------------------------------------------- selection
class SelectionSection(Section):
    top_n: int = Field(10, ge=1, le=100)
    min_score: float = Field(55.0, ge=0, le=100)
    hysteresis_points: float = Field(5.0, ge=0)
    rank_buffer: int = Field(3, ge=0)
    whitelist_policy: Literal["priority", "normal"] = "priority"
    whitelist_min_score: float = Field(40.0, ge=0, le=100)
    whitelist_counts_toward_top_n: bool = True
    alert_on_watchlist: bool = True
    alert_on_observe: bool = False
    interval_seconds: int = Field(300, ge=10)


class SignalsSection(Section):
    min_source_value_usd: float = Field(20.0, ge=0)
    queue_size: int = Field(2000, ge=10)
    partitions: int = Field(8, ge=1, le=128)
    dedupe_cache_size: int = Field(50_000, ge=100)
    follow_sells: bool = True
    late_sell_max_age_minutes: float = Field(1440.0, gt=0)


# --------------------------------------------------------------------------- risk
class RiskSection(Section):
    capital_usd: float = Field(1000.0, gt=0)
    max_risk_per_trade_pct: float = Field(1.0, gt=0, le=25)
    max_risk_per_wallet_pct: float = Field(3.0, gt=0, le=100)
    max_total_exposure_pct: float = Field(50.0, gt=0, le=HL.HARD_MAX_TOTAL_EXPOSURE_PCT)
    max_daily_loss_pct: float = Field(5.0, gt=0, le=HL.HARD_MAX_DAILY_LOSS_PCT)
    max_weekly_loss_pct: float = Field(10.0, gt=0, le=100)
    max_monthly_loss_pct: float = Field(20.0, gt=0, le=100)
    max_open_positions: int = Field(10, ge=1, le=HL.HARD_MAX_OPEN_POSITIONS)
    max_trade_usd: float = Field(100.0, gt=0, le=HL.HARD_MAX_TRADE_USD)
    min_trade_usd: float = Field(10.0, gt=0)
    max_token_exposure_pct: float = Field(10.0, gt=0, le=100)
    max_high_risk_exposure_pct: float = Field(10.0, ge=0, le=100)
    max_slippage_pct: float = Field(3.0, gt=0, le=HL.HARD_MAX_ENTRY_SLIPPAGE_PCT)
    min_liquidity_usd: float = Field(50_000.0, ge=0)
    min_market_cap_usd: float = Field(100_000.0, ge=0)
    max_market_cap_usd: float = Field(5_000_000_000.0, gt=0)
    min_token_age_minutes: float = Field(60.0, ge=0)
    max_token_risk_score: float = Field(60.0, ge=0, le=100)
    high_risk_score_threshold: float = Field(35.0, ge=0, le=100)
    high_risk_max_market_cap_usd: float = Field(1_000_000.0, ge=0)
    block_mint_authority: bool = True
    block_freeze_authority: bool = True
    block_dangerous_extensions: bool = True
    allow_add_to_position: bool = False
    reentry_cooldown_minutes: float = Field(60.0, ge=0)
    max_consecutive_losses: int = Field(6, ge=0, description="0 disables")
    max_execution_errors_per_hour: int = Field(5, ge=0, description="0 disables")
    max_data_age_seconds: float = Field(30.0, gt=0)
    kill_on_reconciliation_mismatch: bool = True
    # Reject entries whose fixed round-trip network cost exceeds this % of the position size.
    max_round_trip_cost_pct: float = Field(3.0, gt=0, le=100)
    reserve_sol: float = Field(0.05, ge=HL.HARD_MIN_RESERVE_SOL)
    token_blacklist: list[str] = Field(default_factory=list)
    token_whitelist_only: bool = False
    token_whitelist: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> RiskSection:
        if self.min_trade_usd >= self.max_trade_usd:
            raise ValueError("risk.min_trade_usd must be < risk.max_trade_usd")
        if self.max_trade_usd > self.capital_usd * HL.HARD_MAX_TRADE_FRACTION:
            raise ValueError(
                f"risk.max_trade_usd ({self.max_trade_usd}) exceeds the hard ceiling of "
                f"{HL.HARD_MAX_TRADE_FRACTION:.0%} of capital ({self.capital_usd})"
            )
        if self.min_market_cap_usd >= self.max_market_cap_usd:
            raise ValueError("risk.min_market_cap_usd must be < risk.max_market_cap_usd")
        if not (self.max_daily_loss_pct <= self.max_weekly_loss_pct <= self.max_monthly_loss_pct):
            raise ValueError("loss limits must satisfy daily <= weekly <= monthly")
        return self


class SizingSection(Section):
    method: Literal["risk_based", "fixed"] = "risk_based"
    fixed_size_usd: float = Field(25.0, gt=0)
    confidence_min_mult: float = Field(0.3, gt=0, le=1)
    confidence_full_score: float = Field(90.0, gt=0, le=100)
    target_hourly_vol_pct: float = Field(10.0, gt=0)
    min_vol_mult: float = Field(0.3, gt=0, le=1)
    max_liquidity_fraction_pct: float = Field(1.0, gt=0, le=20)
    slippage_penalty_start_pct: float = Field(1.0, ge=0)
    min_slippage_mult: float = Field(0.4, gt=0, le=1)
    high_risk_mult: float = Field(0.5, gt=0, le=1)
    correlation_penalty: float = Field(0.25, ge=0, le=5)


class LatencySection(Section):
    max_signal_age_seconds: float = Field(20.0, gt=0, description="COPY_DELAY_LIMIT")
    signal_ttl_seconds: float = Field(30.0, gt=0)
    max_price_deviation_pct: float = Field(5.0, gt=0, le=100)
    requote_before_execution: bool = True
    max_quote_age_seconds: float = Field(5.0, gt=0)
    # Per-wallet delay limit: min(max_signal_age_seconds, max(min_signal_age_seconds,
    # max_age_fraction_of_hold × the wallet's median holding time)). A scalper needs a fresh signal.
    per_wallet_max_age: bool = True
    max_age_fraction_of_hold: float = Field(0.1, gt=0, le=1)
    min_signal_age_seconds: float = Field(2.0, gt=0)


class TakeProfitLevel(Section):
    gain_pct: float = Field(gt=0)
    sell_fraction: float = Field(gt=0, le=1, description="fraction of the REMAINING position")


class ExitsSection(Section):
    default_mode: ExitMode = ExitMode.PROTECTED
    close_on_source_sell: bool = False
    mirror_full_exit_threshold: float = Field(0.9, gt=0, le=1)
    stop_loss_pct: float = Field(20.0, gt=0, lt=100)
    take_profit_levels: list[TakeProfitLevel] = Field(
        default_factory=lambda: [
            TakeProfitLevel(gain_pct=50.0, sell_fraction=0.5),
            TakeProfitLevel(gain_pct=150.0, sell_fraction=1.0),
        ]
    )
    trailing_stop_pct: float | None = Field(15.0, gt=0, lt=100)
    trailing_activation_pct: float = Field(20.0, ge=0)
    max_hold_minutes: float | None = Field(1440.0, gt=0)
    emergency_stop_loss_pct: float = Field(50.0, gt=0, lt=100)
    price_poll_seconds: float = Field(5.0, gt=0)
    stale_price_alert_seconds: float = Field(120.0, gt=0)
    exit_slippage_pct: float = Field(10.0, gt=0, le=HL.HARD_MAX_EXIT_SLIPPAGE_PCT)

    @field_validator("take_profit_levels")
    @classmethod
    def _sorted(cls, levels: list[TakeProfitLevel]) -> list[TakeProfitLevel]:
        gains = [lvl.gain_pct for lvl in levels]
        if gains != sorted(gains) or len(set(gains)) != len(gains):
            raise ValueError("take_profit_levels must have strictly increasing gain_pct")
        return levels

    @model_validator(mode="after")
    def _stops(self) -> ExitsSection:
        if self.emergency_stop_loss_pct < self.stop_loss_pct:
            raise ValueError("exits.emergency_stop_loss_pct must be >= exits.stop_loss_pct")
        return self


class ExecutionSection(Section):
    quote_mint: str = SOL_MINT
    wallet_public_key: str | None = None
    slippage_bps: int = Field(150, ge=1, le=int(HL.HARD_MAX_ENTRY_SLIPPAGE_PCT * 100))
    priority_fee_max_lamports: int = Field(1_000_000, ge=0, le=50_000_000)
    priority_level: Literal["medium", "high", "veryHigh"] = "veryHigh"
    confirm_timeout_seconds: float = Field(60.0, gt=0)
    rebroadcast_interval_ms: int = Field(1500, ge=200)
    skip_preflight: bool = True
    jito_tip_lamports: int = Field(0, ge=0, le=10_000_000)
    entry_max_attempts: int = Field(2, ge=1, le=5)
    exit_max_attempts: int = Field(5, ge=1, le=20)
    reconcile_interval_seconds: float = Field(30.0, gt=0)
    # Priority fee actually expected per transaction (paper costs, backtest, cost filter).
    # None = assume the configured maximum (conservative).
    expected_priority_fee_lamports: int | None = Field(None, ge=0, le=50_000_000)
    # Close empty token accounts after a position is closed to recover their rent (~0.002 SOL each).
    close_empty_token_accounts: bool = True
    close_accounts_interval_seconds: float = Field(120.0, ge=30)

    @model_validator(mode="after")
    def _expected_fee_within_max(self) -> ExecutionSection:
        if (
            self.expected_priority_fee_lamports is not None
            and self.expected_priority_fee_lamports > self.priority_fee_max_lamports
        ):
            raise ValueError("execution.expected_priority_fee_lamports cannot exceed priority_fee_max_lamports")
        return self


class PaperSection(Section):
    simulated_latency_ms: float = Field(800.0, ge=0)
    extra_slippage_bps: float = Field(50.0, ge=0)
    # Base network fee per transaction (one signature = 5,000 lamports). Priority fee,
    # Jito tip and token-account rent are added from the execution settings.
    network_fee_sol: float = Field(0.000005, ge=0)
    use_real_quotes: bool = True


class Level4Caps(Section):
    max_trade_usd: float = Field(20.0, gt=0, le=HL.HARD_LEVEL4_MAX_TRADE_USD)
    max_open_positions: int = Field(3, ge=1, le=HL.HARD_LEVEL4_MAX_OPEN_POSITIONS)
    max_total_exposure_pct: float = Field(20.0, gt=0, le=100)
    max_daily_loss_pct: float = Field(2.0, gt=0, le=100)
    max_risk_per_trade_pct: float = Field(0.5, gt=0, le=25)


class LevelsSection(Section):
    level4: Level4Caps = Level4Caps()
    live_trading_enabled: bool = False
    require_arm: bool = True
    preflight_min_paper_days: int = Field(7, ge=0)


# ------------------------------------------------------------------ notifications
def _default_alert_events() -> dict[AlertType, bool]:
    return {t: True for t in AlertType}


class NotificationsSection(Section):
    telegram_enabled: bool = False
    telegram_chat_id: str | None = None
    discord_enabled: bool = False
    min_severity: Severity = Severity.INFO
    events: dict[AlertType, bool] = Field(default_factory=_default_alert_events)
    dedupe_window_seconds: float = Field(60.0, ge=0)
    max_per_minute: int = Field(20, ge=1)


# ---------------------------------------------------------------------- api / ops
class ApiSection(Section):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(8080, ge=1, le=65535)
    session_ttl_minutes: int = Field(720, ge=5)
    login_max_attempts: int = Field(5, ge=1)
    login_lockout_minutes: int = Field(15, ge=1)
    rate_limit_per_minute: int = Field(240, ge=10)
    secure_cookies: bool = True
    require_totp: bool = False


class ObservabilitySection(Section):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_logs: bool = True
    metrics_enabled: bool = True
    metrics_host: str = "127.0.0.1"
    metrics_port: int = Field(9464, ge=1, le=65535)
    event_log_retention_days: int = Field(90, ge=1)
    equity_snapshot_seconds: int = Field(60, ge=5)


class SecuritySection(Section):
    signer_mode: Literal["none", "remote", "local"] = "none"
    signer_url: str = "http://signer:8700"
    keystore_path: str = "secrets/bot.keystore.json"
    signer_timeout_seconds: float = Field(5.0, gt=0)
    hmac_max_skew_seconds: float = Field(30.0, gt=0, le=300)


class BacktestSection(Section):
    train_days: int = Field(30, ge=1)
    test_days: int = Field(7, ge=1)
    latency_seconds: float = Field(3.0, ge=0)
    entry_slippage_pct: float = Field(2.0, ge=0)
    exit_slippage_pct: float = Field(2.0, ge=0)
    # None = the same network-cost model as paper trading (priority fee/tip + rent), per transaction.
    fee_usd_per_trade: float | None = Field(None, ge=0)
    impact_coefficient: float = Field(1.0, ge=0)


# --------------------------------------------------------------------------- root
class AppConfig(Section):
    app: AppSection = AppSection()
    wallets: WalletsSection = WalletsSection()
    providers: ProvidersSection = ProvidersSection()
    analysis: AnalysisSection = AnalysisSection()
    scoring: ScoringSection = ScoringSection()
    status_rules: StatusRulesSection = StatusRulesSection()
    detection: DetectionSection = DetectionSection()
    selection: SelectionSection = SelectionSection()
    signals: SignalsSection = SignalsSection()
    risk: RiskSection = RiskSection()
    sizing: SizingSection = SizingSection()
    latency: LatencySection = LatencySection()
    exits: ExitsSection = ExitsSection()
    execution: ExecutionSection = ExecutionSection()
    paper: PaperSection = PaperSection()
    levels: LevelsSection = LevelsSection()
    notifications: NotificationsSection = NotificationsSection()
    api: ApiSection = ApiSection()
    observability: ObservabilitySection = ObservabilitySection()
    security: SecuritySection = SecuritySection()
    backtest: BacktestSection = BacktestSection()

    @model_validator(mode="after")
    def _cross_section(self) -> AppConfig:
        if self.execution.slippage_bps / 100.0 > self.risk.max_slippage_pct:
            raise ValueError("execution.slippage_bps must not exceed risk.max_slippage_pct")
        if self.selection.top_n > self.wallets.max_wallets:
            raise ValueError("selection.top_n cannot exceed wallets.max_wallets")
        if self.app.operating_level.is_live:
            if self.providers.mode != "live":
                raise ValueError("live operating levels require providers.mode = live")
            if self.security.signer_mode == "none":
                raise ValueError("live operating levels require a signer (security.signer_mode)")
            if not self.execution.wallet_public_key:
                raise ValueError("live operating levels require execution.wallet_public_key")
        return self
