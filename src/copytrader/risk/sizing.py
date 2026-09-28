"""Position sizing.

Copying a trade never means using the source wallet's amount. Size is:

    base  = sizing capital × risk per trade / stop distance          (risk-based)
    × confidence(score) × volatility adj. × slippage adj. × asset-risk adj.
    × correlation penalty (open positions in the same category)
    → capped by: max trade, pool liquidity share, remaining total / token /
      wallet / high-risk capacity, and the absolute hard cap.

Every step is recorded so the dashboard can show *why* the size is what it is.
A result below the minimum useful size (fees would dominate) is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from copytrader.analysis.stats import clip
from copytrader.config.models import SizingSection


@dataclass(frozen=True, slots=True)
class SizingInput:
    sizing_capital_usd: float
    max_risk_per_trade_pct: float
    stop_distance_pct: float
    wallet_score: float | None
    min_score: float
    hourly_volatility: float | None
    liquidity_usd: float | None
    est_slippage_pct: float | None
    max_slippage_pct: float
    is_high_risk: bool
    same_category_positions: int
    max_trade_usd: float
    min_trade_usd: float
    hard_cap_usd: float
    total_capacity_usd: float
    token_capacity_usd: float
    wallet_risk_capacity_usd: float  # remaining at-risk budget for the source wallet
    high_risk_capacity_usd: float
    # Signal-level adjustments (confluence, market regime): (name, label, factor), before the caps
    extra_factors: tuple[tuple[str, str, float], ...] = ()


@dataclass(slots=True)
class SizingResult:
    size_usd: float
    limited_by: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    rejected_reason: str | None = None
    limited_by_label: str = "modelo de riesgo (sin topes)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "size_usd": round(self.size_usd, 2),
            "limited_by": self.limited_by,
            "limited_by_label": self.limited_by_label,
            "steps": self.steps,
            "rejected_reason": self.rejected_reason,
        }


def compute_size(inp: SizingInput, cfg: SizingSection) -> SizingResult:
    steps: list[dict[str, Any]] = []

    def step(name: str, label: str, size: float, factor: float | None = None) -> float:
        steps.append(
            {
                "name": name,
                "label": label,
                "factor": None if factor is None else round(factor, 4),
                "size_usd": round(size, 2),
            }
        )
        return size

    stop = max(inp.stop_distance_pct, 1.0)
    if cfg.method == "fixed":
        size = step("base", "Tamaño fijo", cfg.fixed_size_usd)
    else:
        size = step(
            "base",
            f"Riesgo {inp.max_risk_per_trade_pct}% del capital / stop {stop:.0f}%",
            inp.sizing_capital_usd * inp.max_risk_per_trade_pct / stop,
        )

    score = inp.wallet_score if inp.wallet_score is not None else inp.min_score
    span = max(1e-9, cfg.confidence_full_score - inp.min_score)
    conf = cfg.confidence_min_mult + (1 - cfg.confidence_min_mult) * clip((score - inp.min_score) / span)
    size = step("confidence", f"Confianza por score {score:.0f}", size * conf, conf)

    if inp.hourly_volatility is not None and inp.hourly_volatility > 0:
        vol_mult = clip(cfg.target_hourly_vol_pct / 100 / inp.hourly_volatility, cfg.min_vol_mult, 1.0)
        size = step("volatility", f"Volatilidad {inp.hourly_volatility * 100:.1f}%/h", size * vol_mult, vol_mult)

    if inp.est_slippage_pct is not None and inp.est_slippage_pct > cfg.slippage_penalty_start_pct:
        rng = max(1e-9, inp.max_slippage_pct - cfg.slippage_penalty_start_pct)
        frac = clip((inp.est_slippage_pct - cfg.slippage_penalty_start_pct) / rng)
        slip_mult = 1 - frac * (1 - cfg.min_slippage_mult)
        size = step("slippage", f"Slippage estimado {inp.est_slippage_pct:.2f}%", size * slip_mult, slip_mult)

    if inp.is_high_risk:
        size = step("asset_risk", "Activo de alto riesgo", size * cfg.high_risk_mult, cfg.high_risk_mult)

    if inp.same_category_positions > 0:
        corr = 1 / (1 + cfg.correlation_penalty * inp.same_category_positions)
        size = step("correlation", f"{inp.same_category_positions} posición(es) correlacionadas", size * corr, corr)

    for name, label, factor in inp.extra_factors:
        size = step(name, label, size * factor, factor)

    caps: list[tuple[str, str, float]] = [
        ("max_trade", "Máximo por operación", inp.max_trade_usd),
        ("hard_cap", "Límite absoluto codificado", inp.hard_cap_usd),
        ("total_exposure", "Exposición total disponible", inp.total_capacity_usd),
        ("token_exposure", "Exposición por token disponible", inp.token_capacity_usd),
        ("wallet_risk", "Riesgo por wallet disponible", inp.wallet_risk_capacity_usd * 100 / stop),
    ]
    if inp.liquidity_usd is not None:
        caps.append(
            (
                "liquidity",
                f"{cfg.max_liquidity_fraction_pct}% de la liquidez",
                inp.liquidity_usd * cfg.max_liquidity_fraction_pct / 100,
            )
        )
    if inp.is_high_risk:
        caps.append(("high_risk", "Exposición alto riesgo disponible", inp.high_risk_capacity_usd))
    limited_by, limited_label = "model", "modelo de riesgo (sin topes)"
    for name, label, cap in caps:
        if size > cap:
            size = step(name, label, max(0.0, cap))
            limited_by, limited_label = name, label.lower()

    result = SizingResult(size_usd=max(0.0, size), limited_by=limited_by, steps=steps, limited_by_label=limited_label)
    if result.size_usd < inp.min_trade_usd:
        result.rejected_reason = (
            f"Tamaño {result.size_usd:.2f} USD < mínimo útil {inp.min_trade_usd:.2f} USD "
            f"(limitado por: {limited_label})"
        )
    return result


def estimate_slippage_pct(size_usd: float, liquidity_usd: float | None) -> float | None:
    """Constant-product estimate of price impact for a buy of ``size_usd``."""
    if not liquidity_usd or liquidity_usd <= 0:
        return None
    reserve = liquidity_usd / 2
    return 100 * size_usd / (reserve + size_usd)
