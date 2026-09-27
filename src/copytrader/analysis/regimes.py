"""Market regime classification from the SOL/USD hourly series.

Regime at time t (using only data up to t — no look-ahead):
* ``extreme_up`` / ``extreme_down`` if |24 h change| ≥ high-vol threshold;
* ``bull`` / ``bear`` if |24 h change| ≥ trend threshold;
* ``sideways`` otherwise.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from datetime import datetime, timedelta

EXTREME_REGIMES = frozenset({"extreme_up", "extreme_down"})


class RegimeClassifier:
    def __init__(self, series: Sequence[tuple[datetime, float]], *, trend_threshold_pct: float,
                 extreme_threshold_pct: float) -> None:
        pairs = sorted(series)
        self._times = [t for t, _ in pairs]
        self._prices = [p for _, p in pairs]
        self.trend = trend_threshold_pct / 100
        self.extreme = extreme_threshold_pct / 100

    def _price_at(self, ts: datetime) -> float | None:
        idx = bisect_right(self._times, ts) - 1
        if idx < 0:
            return None
        if ts - self._times[idx] > timedelta(hours=6):
            return None
        return self._prices[idx]

    def regime(self, ts: datetime) -> str | None:
        now = self._price_at(ts)
        before = self._price_at(ts - timedelta(hours=24))
        if not now or not before:
            return None
        change = now / before - 1
        if change >= self.extreme:
            return "extreme_up"
        if change <= -self.extreme:
            return "extreme_down"
        if change >= self.trend:
            return "bull"
        if change <= -self.trend:
            return "bear"
        return "sideways"
