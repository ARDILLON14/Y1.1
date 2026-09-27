"""Wallet Analyzer: swaps → round trips → metrics for the three windows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog

from copytrader.analysis.metrics import WalletMetrics, compute_metrics, forward_win_rates
from copytrader.analysis.reconstruction import Reconstruction, reconstruct
from copytrader.analysis.regimes import RegimeClassifier
from copytrader.analysis.stats import decay_weights
from copytrader.config.models import AppConfig
from copytrader.core.models import ClosedTrade, SwapEvent
from copytrader.core.types import Side

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class TokenContext:
    """What detectors need to know about a token (from the tokens table)."""

    mint: str
    category: str = "unknown"
    liquidity_usd: float | None = None
    market_cap_usd: float | None = None
    risk_score: float | None = None
    is_rugged: bool = False
    pair_created_at: datetime | None = None


@dataclass(slots=True)
class WalletAnalysis:
    wallet_id: int
    address: str
    swaps: list[SwapEvent]
    recon: Reconstruction
    all: WalletMetrics
    decayed: WalletMetrics
    recent: WalletMetrics
    recent_trades: list[ClosedTrade] = field(default_factory=list)
    older_trades: list[ClosedTrade] = field(default_factory=list)


PriceAt = Callable[[str, datetime], float | None]


class WalletAnalyzer:
    def __init__(self, config: Callable[[], AppConfig]) -> None:
        self._config = config

    def analyze(
        self,
        wallet_id: int,
        address: str,
        swaps: list[SwapEvent],
        *,
        now: datetime,
        tokens: dict[str, TokenContext],
        current_prices: dict[str, float],
        regimes: RegimeClassifier | None = None,
        price_at: PriceAt | None = None,
    ) -> WalletAnalysis:
        cfg = self._config()
        a, s = cfg.analysis, cfg.scoring
        recon = reconstruct(
            address, swaps, dust_fraction=a.dust_fraction, min_trade_usd=a.min_trade_usd,
            stale_before=now - timedelta(days=a.stale_position_days),
            category_of=lambda mint: tokens[mint].category if mint in tokens else None,
            regime_of=regimes.regime if regimes else None,
        )
        for lot in recon.open_lots:
            lot.mark_price_usd = current_prices.get(lot.token_mint)
        trades = recon.closed
        swap_times = [sw.block_time for sw in swaps]
        fwd: dict[str, float] = {}
        if price_at is not None:
            buys = [(sw.token_mint, sw.block_time, sw.price_usd) for sw in swaps
                    if sw.side is Side.BUY and sw.price_usd]
            fwd = forward_win_rates(buys, price_at, a.holding_buckets_minutes, now)  # type: ignore[arg-type]
        common = {
            "now": now, "z": s.sample.confidence_z, "pf_prior_trades": s.sample.profit_factor_prior_trades,
            "holding_edges_minutes": a.holding_buckets_minutes,
            "fast_trade_max_minutes": a.fast_trade_max_minutes,
            "min_replicable_hold_seconds": a.min_replicable_hold_seconds,
            "peak_deployed_usd": recon.peak_deployed_usd,
            "outlier_multiple": cfg.detection.outlier_return_multiple,
        }
        m_all = compute_metrics(trades, window="all", open_lots=recon.open_lots, n_swaps=recon.n_swaps,
                                n_buys=recon.n_buys, n_sells=recon.n_sells,
                                unmatched_sells=recon.unmatched_sells, swap_times=swap_times,
                                forward_win_rates=fwd, **common)
        ages = [(now - t.closed_at).total_seconds() / 86400 for t in trades]
        weights = decay_weights(ages, a.decay_half_life_days) if trades else None
        m_dec = compute_metrics(trades, window="decayed", open_lots=recon.open_lots, weights=weights,
                                swap_times=swap_times, **common)
        recent = trades[-a.recent_trades:]
        older = trades[:-a.recent_trades] if len(trades) > a.recent_trades else []
        recent_times = [t.opened_at for t in recent] + [t.closed_at for t in recent]
        m_rec = compute_metrics(recent, window="recent", swap_times=recent_times, **common)
        return WalletAnalysis(wallet_id=wallet_id, address=address, swaps=swaps, recon=recon, all=m_all,
                              decayed=m_dec, recent=m_rec, recent_trades=recent, older_trades=older)
