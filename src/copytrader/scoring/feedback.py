"""Learning from our own copies.

The copy estimate (analysis/replication.py) is a model. Once we have copied a
wallet, its closed positions tell us what copying it really returns. The
*effective* copy expectancy blends both, like a Bayesian update:

    effective = (k · estimate + n · realized) / (k + n)

``k`` (``learning.feedback_prior_positions``) says how many real copies the
model is worth: with few real copies the estimate dominates, with many the
reality does. Scoring and status rules use the effective value, so a wallet
that looks good on paper but loses when copied is demoted automatically.

Returns are net of fees (the positions' realized PnL over their cost), from both
paper and live copies: paper copies use real quotes and the same costs model.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from copytrader.analysis import stats
from copytrader.analysis.metrics import WalletMetrics


@dataclass(frozen=True, slots=True)
class RealizedCopy:
    n: int
    mean_pct: float
    lb_pct: float
    ub_pct: float


def realized_stats(returns: Sequence[float], z: float = 1.645) -> RealizedCopy | None:
    if not returns:
        return None
    mean = stats.mean(returns) or 0.0
    sd = stats.std(returns) or 0.0
    half = z * sd / math.sqrt(len(returns)) if len(returns) > 1 else float("inf")
    return RealizedCopy(
        n=len(returns),
        mean_pct=100 * mean,
        lb_pct=100 * (mean - half) if math.isfinite(half) else -math.inf,
        ub_pct=100 * (mean + half) if math.isfinite(half) else math.inf,
    )


def _blend(estimate: float | None, realized: float, n: int, k: float) -> float:
    if estimate is None or k <= 0:
        return realized
    return (k * estimate + n * realized) / (k + n)


def apply_feedback(m: WalletMetrics, realized: RealizedCopy | None, prior_positions: float) -> None:
    """Fill the realized/effective copy fields of ``m`` (no-op without real copies)."""
    if realized is None:
        m.effective_copy_expectancy_pct = m.copy_expectancy_pct
        m.effective_copy_expectancy_lb_pct = m.copy_expectancy_lb_pct
        return
    m.realized_copy_n = realized.n
    m.realized_copy_mean_pct = realized.mean_pct
    m.realized_copy_ub_pct = realized.ub_pct if math.isfinite(realized.ub_pct) else None
    m.effective_copy_expectancy_pct = _blend(m.copy_expectancy_pct, realized.mean_pct, realized.n, prior_positions)
    lb = realized.lb_pct if math.isfinite(realized.lb_pct) else realized.mean_pct
    m.effective_copy_expectancy_lb_pct = _blend(m.copy_expectancy_lb_pct, lb, realized.n, prior_positions)
