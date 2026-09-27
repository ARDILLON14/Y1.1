"""Pure statistical helpers (no I/O, fully unit-tested).

Robustness choices:
* win rates are judged by their **Wilson lower bound**, not the raw ratio;
* expectancy by a **lower confidence bound** of the mean;
* weighted statistics use Kish's effective sample size so time-decayed
  weights cannot fake a large sample.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def norm01(x: float | None, lo: float, hi: float, *, invert: bool = False) -> float | None:
    """Linear map of ``x`` from [lo, hi] to [0, 1] (clipped); ``invert`` flips it."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    if hi == lo:
        return 0.5
    v = clip((x - lo) / (hi - lo))
    return 1.0 - v if invert else v


def mean(values: Sequence[float], weights: Sequence[float] | None = None) -> float | None:
    if not values:
        return None
    if weights is None:
        return sum(values) / len(values)
    sw = sum(weights)
    return sum(v * w for v, w in zip(values, weights, strict=True)) / sw if sw > 0 else None


def std(values: Sequence[float], weights: Sequence[float] | None = None) -> float | None:
    """Sample standard deviation (weighted: reliability-weights correction)."""
    n = len(values)
    if n < 2:
        return None
    m = mean(values, weights)
    assert m is not None
    if weights is None:
        return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))
    v1 = sum(weights)
    v2 = sum(w * w for w in weights)
    denom = v1 - v2 / v1 if v1 > 0 else 0
    if denom <= 0:
        return None
    return math.sqrt(sum(w * (v - m) ** 2 for v, w in zip(values, weights, strict=True)) / denom)


def median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def effective_n(weights: Sequence[float] | None, n: int) -> float:
    if weights is None:
        return float(n)
    s1 = sum(weights)
    s2 = sum(w * w for w in weights)
    return (s1 * s1) / s2 if s2 > 0 else 0.0


def wilson_lower_bound(successes: float, n: float, z: float = 1.645) -> float | None:
    """Lower bound of the Wilson score interval for a binomial proportion."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def mean_lower_bound(values: Sequence[float], z: float = 1.645, weights: Sequence[float] | None = None) -> float | None:
    """``mean − z·σ/√n_eff``: penalises small or noisy samples."""
    m = mean(values, weights)
    s = std(values, weights)
    if m is None:
        return None
    if s is None:
        return m - abs(m)  # a single observation proves nothing
    n = effective_n(weights, len(values))
    return m - z * s / math.sqrt(n) if n > 0 else None


def max_drawdown(equity: Sequence[float]) -> tuple[float, float]:
    """Return (max drawdown fraction, max drawdown absolute) of an equity curve."""
    peak = -math.inf
    worst_frac = 0.0
    worst_abs = 0.0
    for value in equity:
        peak = max(peak, value)
        dd_abs = peak - value
        if dd_abs > worst_abs:
            worst_abs = dd_abs
        if peak > 0:
            worst_frac = max(worst_frac, min(1.0, dd_abs / peak))
    return worst_frac, worst_abs


def max_streak(flags: Sequence[bool], target: bool = True) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f is target else 0
        best = max(best, cur)
    return best


def sharpe(values: Sequence[float]) -> float | None:
    m, s = mean(values), std(values)
    if m is None or not s:
        return None
    return m / s


def sortino(values: Sequence[float]) -> float | None:
    m = mean(values)
    if m is None or len(values) < 2:
        return None
    downside = [min(0.0, v) ** 2 for v in values]
    dd = math.sqrt(sum(downside) / (len(values) - 1))
    return m / dd if dd > 0 else None


def hhi(shares: Sequence[float]) -> float:
    """Herfindahl index of non-negative shares (normalised to sum 1)."""
    total = sum(shares)
    if total <= 0:
        return 0.0
    return sum((s / total) ** 2 for s in shares)


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def two_proportion_z(k1: float, n1: float, k2: float, n2: float) -> tuple[float, float] | None:
    """One-sided test that proportion 2 (recent) is *lower* than proportion 1.

    Returns (z, p_value). ``None`` if not computable.
    """
    if n1 <= 0 or n2 <= 0:
        return None
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    z = (p1 - p2) / se
    return z, 1 - normal_cdf(z)


def decay_weights(ages_days: Sequence[float], half_life_days: float) -> list[float]:
    lam = math.log(2) / half_life_days
    return [math.exp(-lam * max(0.0, a)) for a in ages_days]


def safe_ratio(a: float, b: float, cap: float = 99.0) -> float | None:
    if b == 0:
        return cap if a > 0 else None
    return min(cap, a / b)
