"""Wallet Scoring Engine.

score = shrink(historical) blended with shrink(recent), minus penalties.

1. Each component is a robust metric mapped to [0, 1] with configurable bounds
   (lower confidence bounds instead of raw averages wherever possible).
2. Raw score = weighted mean of available components (weights re-normalised
   when a component cannot be computed — e.g. no extreme-market trades).
3. Bayesian shrinkage: ``prior + (raw − prior) · n / (n + k)``. A wallet with
   few trades is pulled toward a neutral prior and cannot rank on luck.
4. Historical (time-decayed) and recent (last N trades) scores are blended;
   the recent weight scales with how many recent trades exist.
5. Penalties: warnings subtract points (capped), a critical flag caps the score.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from copytrader.analysis.analyzer import WalletAnalysis
from copytrader.analysis.metrics import WalletMetrics
from copytrader.analysis.stats import clip, norm01
from copytrader.config.models import AppConfig, ScoringBounds
from copytrader.core.models import Flag
from copytrader.core.types import Severity

CRITICAL_SCORE_CAP = 20.0

COMPONENT_LABELS = {
    "profitability": "Rentabilidad",
    "consistency": "Consistencia",
    "drawdown": "Drawdown",
    "win_rate": "Win rate (límite inferior)",
    "profit_factor": "Profit factor (contraído)",
    "risk": "Riesgo por operación",
    "volatility": "Volatilidad de resultados",
    "sample_size": "Tamaño de muestra",
    "activity": "Actividad",
    "concentration": "Diversificación del beneficio",
    "extreme_moves": "Mercado extremo",
    "replicability": "Replicabilidad",
    "copy_edge": "Ventaja copiable",
}


@dataclass
class ScoreResult:
    score: float
    score_hist: float | None
    score_recent: float | None
    raw_hist: float | None
    confidence: float
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    penalties: list[dict[str, Any]] = field(default_factory=list)
    recent_weight: float = 0.0


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def compute_components(m: WalletMetrics, b: ScoringBounds, k: float) -> dict[str, tuple[float | None, Any]]:
    """Return {component: (normalised value or None, raw input for display)}."""
    comps: dict[str, tuple[float | None, Any]] = {}
    exp_lb = norm01(m.expectancy_lb_pct, b.expectancy_lo_pct, b.expectancy_hi_pct)
    roi = norm01(m.roi_pct, b.roi_lo_pct, b.roi_hi_pct)
    comps["profitability"] = (
        None if exp_lb is None else 0.6 * exp_lb + 0.4 * (roi if roi is not None else exp_lb),
        {"expectancy_lb_pct": m.expectancy_lb_pct, "roi_pct": m.roi_pct},
    )
    comps["consistency"] = (
        _mean([norm01(m.profitable_weeks_frac, 0.2, b.profitable_weeks_hi), norm01(m.profitable_days_frac, 0.2, 0.7)]),
        {"profitable_weeks": m.profitable_weeks_frac, "profitable_days": m.profitable_days_frac},
    )
    comps["drawdown"] = (
        norm01(m.max_drawdown_pct, 0.0, b.drawdown_max_pct, invert=True),
        {"max_drawdown_pct": m.max_drawdown_pct},
    )
    comps["win_rate"] = (
        norm01(m.win_rate_lb, b.win_rate_lo, b.win_rate_hi),
        {"win_rate": m.win_rate, "win_rate_lb": m.win_rate_lb},
    )
    pf = m.profit_factor_shrunk
    comps["profit_factor"] = (
        None if pf is None else clip(math.log(max(pf, 1e-9)) / math.log(b.profit_factor_hi)),
        {"profit_factor": m.profit_factor, "profit_factor_shrunk": pf},
    )
    comps["risk"] = (
        _mean(
            [
                norm01(m.avg_loss_pct, 0.0, b.avg_loss_hi_pct, invert=True),
                None
                if m.worst_trade_pct is None
                else norm01(-m.worst_trade_pct, 0.0, b.worst_loss_hi_pct, invert=True),
            ]
        ),
        {"avg_loss_pct": m.avg_loss_pct, "worst_trade_pct": m.worst_trade_pct},
    )
    comps["volatility"] = (
        norm01(m.return_std_pct, 0.0, b.return_std_hi_pct, invert=True),
        {"return_std_pct": m.return_std_pct},
    )
    comps["sample_size"] = (
        1 - math.exp(-m.n_effective / k) if m.n_closed_trades else 0.0,
        {"n_trades": m.n_closed_trades, "n_effective": round(m.n_effective, 1)},
    )
    recency = norm01(m.days_since_last_trade, 0.0, b.inactive_days_zero, invert=True)
    tpd = m.trades_per_day
    if tpd is None:
        freq = None
    elif tpd > b.trades_per_day_max:
        freq = norm01(tpd, b.trades_per_day_max, 3 * b.trades_per_day_max, invert=True)
    else:
        freq = clip(tpd / 0.1)
    comps["activity"] = (_mean([recency, freq]), {"days_since_last": m.days_since_last_trade, "trades_per_day": tpd})
    top = m.concentration.get("top_trade_share")
    hhi = m.concentration.get("token_hhi")
    comps["concentration"] = (
        None if top is None else 0.7 * (1 - top) + 0.3 * (1 - (hhi or 0.0)),
        {"top_trade_share": top, "token_hhi": hhi},
    )
    ext = m.extreme_moves
    if ext and ext.get("n", 0) >= 3 and m.expectancy_pct is not None:
        diff = (ext.get("avg_return_pct") or 0.0) - m.expectancy_pct
        comps["extreme_moves"] = (norm01(diff, -20.0, 10.0), {"n": ext.get("n"), "diff_pct": diff})
    else:
        comps["extreme_moves"] = (None, {"n": (ext or {}).get("n", 0)})
    comps["replicability"] = (m.replicable_frac, {"replicable_frac": m.replicable_frac})
    # What copying the wallet would return with our latency, size and costs (lower bound + mean).
    # Once we have copied the wallet, the estimate is blended with the real result (scoring/feedback.py).
    mean = m.effective_copy_expectancy_pct if m.effective_copy_expectancy_pct is not None else m.copy_expectancy_pct
    low = (
        m.effective_copy_expectancy_lb_pct
        if m.effective_copy_expectancy_lb_pct is not None
        else (m.copy_expectancy_lb_pct)
    )
    copy_lb = norm01(low, b.copy_expectancy_lo_pct, b.copy_expectancy_hi_pct)
    copy_mean = norm01(mean, b.copy_expectancy_lo_pct, b.copy_expectancy_hi_pct)
    comps["copy_edge"] = (
        None if copy_lb is None or copy_mean is None else 0.5 * copy_lb + 0.5 * copy_mean,
        {
            "copy_expectancy_pct": m.copy_expectancy_pct,
            "copy_expectancy_lb_pct": m.copy_expectancy_lb_pct,
            "copy_cost_pct": m.copy_cost_pct,
            "n": m.copy_n,
            "realized_copy_n": m.realized_copy_n,
            "realized_copy_mean_pct": m.realized_copy_mean_pct,
            "effective_copy_expectancy_pct": m.effective_copy_expectancy_pct,
        },
    )
    return comps


class ScoringEngine:
    def __init__(self, config: Callable[[], AppConfig]) -> None:
        self._config = config

    def _raw(self, comps: dict[str, tuple[float | None, Any]]) -> float | None:
        weights = self._config().scoring.weights.model_dump()
        total_w = 0.0
        acc = 0.0
        for name, (value, _) in comps.items():
            w = weights.get(name, 0.0)
            if value is None or w <= 0:
                continue
            acc += w * clip(value)
            total_w += w
        return 100 * acc / total_w if total_w > 0 else None

    def _shrink(self, raw: float | None, n: float) -> tuple[float, float]:
        sample = self._config().scoring.sample
        confidence = n / (n + sample.prior_trades) if n > 0 else 0.0
        if raw is None:
            return sample.prior_score * 0.5, confidence
        return sample.prior_score + (raw - sample.prior_score) * confidence, confidence

    def score(self, analysis: WalletAnalysis, flags: list[Flag]) -> ScoreResult:
        cfg = self._config().scoring
        k = cfg.sample.prior_trades
        comps_hist = compute_components(analysis.decayed, cfg.bounds, k)
        raw_hist = self._raw(comps_hist)
        hist, confidence = self._shrink(raw_hist, analysis.decayed.n_effective)

        recent_n = analysis.recent.n_closed_trades
        recent: float | None = None
        w_recent = 0.0
        if recent_n > 0 and len(analysis.older_trades) > 0:
            comps_recent = compute_components(analysis.recent, cfg.bounds, k)
            recent, _ = self._shrink(self._raw(comps_recent), recent_n)
            full = self._config().analysis.recent_trades
            w_recent = cfg.recent_weight * min(1.0, recent_n / full)
        score = hist if recent is None else (1 - w_recent) * hist + w_recent * recent

        penalties: list[dict[str, Any]] = []
        warn_points = 0.0
        for flag in flags:
            if flag.severity is Severity.WARNING:
                pts = cfg.degradation.penalty_points if flag.code == "DEGRADATION" else cfg.warning_penalty_points
                warn_points += pts
                penalties.append({"code": flag.code, "points": pts, "reason": flag.message})
        applied = min(warn_points, cfg.max_warning_penalty_points)
        score -= applied
        if any(f.severity is Severity.CRITICAL for f in flags):
            if score > CRITICAL_SCORE_CAP:
                penalties.append(
                    {
                        "code": "CRITICAL_CAP",
                        "points": round(score - CRITICAL_SCORE_CAP, 2),
                        "reason": "Flag crítico: score limitado",
                    }
                )
            score = min(score, CRITICAL_SCORE_CAP)
        weights = cfg.weights.model_dump()
        components = {
            name: {
                "label": COMPONENT_LABELS.get(name, name),
                "value": None if v is None else round(v, 4),
                "weight": weights.get(name, 0.0),
                "input": inp,
            }
            for name, (v, inp) in comps_hist.items()
        }
        return ScoreResult(
            score=round(clip(score, 0.0, 100.0), 2),
            score_hist=round(hist, 2),
            score_recent=None if recent is None else round(recent, 2),
            raw_hist=None if raw_hist is None else round(raw_hist, 2),
            confidence=round(confidence, 4),
            components=components,
            penalties=penalties,
            recent_weight=round(w_recent, 3),
        )
