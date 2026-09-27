"""Wallet status rules: ACTIVA / OBSERVAR / BLOQUEADA, always with reasons."""

from __future__ import annotations

from dataclasses import dataclass, field

from copytrader.analysis.metrics import WalletMetrics
from copytrader.config.models import StatusRulesSection
from copytrader.core.models import Flag
from copytrader.core.types import ListType, Severity, WalletStatus


@dataclass(slots=True)
class StatusDecision:
    status: WalletStatus
    reasons: list[str] = field(default_factory=list)


def decide_status(
    *, list_type: ListType, score: float, metrics: WalletMetrics, flags: list[Flag], rules: StatusRulesSection
) -> StatusDecision:
    if list_type is ListType.BLACKLIST:
        return StatusDecision(WalletStatus.BLOCKED, ["En blacklist manual: nunca se copiará"])
    critical = [f for f in flags if f.severity is Severity.CRITICAL]
    if critical and rules.block_on_critical_flag:
        return StatusDecision(WalletStatus.BLOCKED, [f"[{f.code}] {f.message}" for f in critical])
    if rules.block_below_score is not None and score < rules.block_below_score:
        return StatusDecision(
            WalletStatus.BLOCKED, [f"Score {score:.1f} < umbral de bloqueo {rules.block_below_score:.0f}"]
        )

    observe: list[str] = []
    if metrics.n_closed_trades < rules.min_trades_active:
        observe.append(
            f"Muestra insuficiente: {metrics.n_closed_trades} operaciones cerradas (mínimo {rules.min_trades_active})"
        )
    if score < rules.min_score_active:
        observe.append(f"Score {score:.1f} < mínimo {rules.min_score_active:.0f}")
    if metrics.days_since_last_trade is not None and metrics.days_since_last_trade > rules.max_inactive_days:
        observe.append(f"Inactiva desde hace {metrics.days_since_last_trade:.0f} días")
    if rules.observe_on_degradation:
        observe.extend(f"[{f.code}] {f.message}" for f in flags if f.code == "DEGRADATION")
    if list_type is ListType.WATCHLIST:
        observe.append("En watchlist: solo alertas, sin copia automática")
    if observe:
        return StatusDecision(WalletStatus.OBSERVE, observe)

    reasons = [f"Score {score:.1f} ≥ {rules.min_score_active:.0f}", f"{metrics.n_closed_trades} operaciones cerradas"]
    reasons.extend(f"Aviso [{f.code}] {f.message}" for f in flags if f.severity is Severity.WARNING)
    return StatusDecision(WalletStatus.ACTIVE, reasons)
