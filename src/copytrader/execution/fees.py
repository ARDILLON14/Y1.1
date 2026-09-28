"""Dynamic network fees: how much priority to pay for each transaction.

Speed costs money, and for small trades the priority fee can eat a large part
of the edge. The policy decides per order:

* **Urgency.** Entries use ``execution.priority_level``. Routine exits (take
  profit, time limit) use ``execution.exit_priority_level``. Protective exits
  (stop loss, trailing, emergency, kill switch, the source selling, manual) and
  any exit that already failed once are urgent: highest level and the full
  absolute cap, because not landing them costs more than the fee.
* **Size.** Outside urgent exits the fee is capped at
  ``execution.priority_fee_max_trade_pct`` of the trade value (with a small
  floor), so a 10 USD copy does not pay the same priority as a 100 USD one.
* **Jito.** With ``execution.jito_tip_lamports > 0`` a tip is paid instead of a
  priority fee (Jupiter adds one or the other). The tip follows the recent
  landed-tip percentile published by Jito (``jito_tip_percentile``), capped by
  the configured maximum and the size cap.

It also keeps the fees our live transactions actually paid: their median
replaces the configured assumptions in the cost model (entry filter, EV, paper
trading and the copy-replication model) once there are enough samples.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any

import structlog

from copytrader.config.models import AppConfig
from copytrader.core.errors import CopyTraderError
from copytrader.core.types import OrderPurpose
from copytrader.execution.costs import (
    LAMPORTS_PER_SOL,
    base_fee_lamports,
    priority_cap_lamports,
    swap_fee_lamports,
)
from copytrader.resilience.http import ResilientHttp
from copytrader.resilience.rate_limiter import Priority

log = structlog.get_logger(__name__)

# Exits that can wait a little: everything else protects capital and must land.
ROUTINE_EXIT_TRIGGERS = frozenset({"take_profit", "max_hold"})
TOP_LEVEL = "veryHigh"
TIP_PERCENTILES = (25, 50, 75, 95, 99)


@dataclass(frozen=True, slots=True)
class FeeDecision:
    priority_level: str
    priority_max_lamports: int  # Jupiter priorityLevelWithMaxLamports.maxLamports (unused with a tip)
    jito_tip_lamports: int  # > 0: pay this tip instead of a priority fee
    urgent: bool
    source: str  # where the amount came from (for the order record)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FeeTracker:
    """Network fees (base + priority fee + tip, lamports) actually paid by live swaps."""

    def __init__(self, window: int = 50) -> None:
        self._samples: deque[int] = deque(maxlen=window)

    def record(self, lamports: int) -> None:
        if lamports > 0:
            self._samples.append(int(lamports))

    def load(self, samples: Iterable[int]) -> None:
        """Seed with past fees, oldest first."""
        for value in samples:
            self.record(value)

    def typical(self, min_samples: int) -> int | None:
        if len(self._samples) < max(min_samples, 1):
            return None
        return int(median(self._samples))

    def __len__(self) -> int:
        return len(self._samples)


class JitoTipFloor:
    """Recently landed Jito tips by percentile (public endpoint), refreshed in the background."""

    def __init__(self, http: ResilientHttp, url: str, *, refresh_seconds: float = 30.0, max_age: float = 300.0):
        self.http = http
        self.url = url
        self.refresh_seconds = refresh_seconds
        self.max_age = max_age
        self._tips: dict[int, int] = {}
        self._at = 0.0
        self._stopped = asyncio.Event()

    def update(self, data: Any, now: float | None = None) -> None:
        row = data[0] if isinstance(data, list) and data else data
        if not isinstance(row, dict):
            return
        tips: dict[int, int] = {}
        for pct in TIP_PERCENTILES:
            value = row.get(f"landed_tips_{pct}th_percentile")
            if not isinstance(value, int | float | str):
                continue
            try:
                sol = float(value)
            except ValueError:
                continue
            if sol >= 0:
                tips[pct] = int(sol * LAMPORTS_PER_SOL)
        if tips:
            self._tips = tips
            self._at = time.monotonic() if now is None else now

    def lamports(self, percentile: int, now: float | None = None) -> int | None:
        """Tip at that percentile, or None when unknown or stale."""
        now = time.monotonic() if now is None else now
        if not self._tips or now - self._at > self.max_age:
            return None
        return self._tips.get(percentile)

    def snapshot(self) -> dict[str, Any]:
        fresh = self._tips and time.monotonic() - self._at <= self.max_age
        return {"percentiles": dict(self._tips) if fresh else {}, "age_seconds": round(time.monotonic() - self._at)}

    async def refresh(self) -> None:
        try:
            self.update(await self.http.get_json(self.url, priority=Priority.BACKGROUND))
        except CopyTraderError as exc:
            log.warning("jito_tip_floor_failed", error=str(exc))

    async def run(self, enabled: Callable[[], bool] = lambda: True) -> None:
        while not self._stopped.is_set():
            if enabled():
                await self.refresh()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self.refresh_seconds)

    async def stop(self) -> None:
        self._stopped.set()


class FeePolicy:
    def __init__(
        self,
        config: Callable[[], AppConfig],
        tracker: FeeTracker | None = None,
        tip_floor: JitoTipFloor | None = None,
    ) -> None:
        self._config = config
        self.tracker = tracker or FeeTracker()
        self.tip_floor = tip_floor

    def _market_tip(self, cfg: AppConfig) -> int | None:
        pct = cfg.execution.jito_tip_percentile
        if cfg.execution.jito_tip_lamports <= 0 or pct is None or self.tip_floor is None:
            return None
        return self.tip_floor.lamports(pct)

    def observed_fee_lamports(self) -> int | None:
        return self.tracker.typical(self._config().execution.fee_min_samples)

    def expected_swap_fee_lamports(self, notional_usd: float | None = None, sol_price: float | None = None) -> int:
        """Network cost we expect for one swap of this size (cost model)."""
        cfg = self._config()
        return swap_fee_lamports(
            cfg,
            notional_usd,
            sol_price,
            observed=self.observed_fee_lamports(),
            market=self._market_tip(cfg),
        )

    def decide(
        self,
        *,
        purpose: OrderPurpose,
        notional_usd: float | None,
        sol_price: float | None,
        trigger: str | None = None,
        attempt: int = 1,
    ) -> FeeDecision:
        cfg = self._config()
        ex = cfg.execution
        urgent = purpose is OrderPurpose.EXIT and (attempt > 1 or trigger not in ROUTINE_EXIT_TRIGGERS)
        routine_level = ex.priority_level if purpose is OrderPurpose.ENTRY else ex.exit_priority_level
        level = TOP_LEVEL if urgent else routine_level
        # urgent exits ignore the size cap: landing them matters more than the fee
        cap = priority_cap_lamports(cfg, None if urgent else notional_usd, sol_price)
        if ex.jito_tip_lamports > 0:
            market = None if urgent else self._market_tip(cfg)
            if market is None:
                return FeeDecision(level, 0, cap, urgent, "jito_max" if urgent else "jito_cap")
            tip = min(cap, max(ex.min_jito_tip_lamports, market))
            return FeeDecision(level, 0, tip, urgent, f"jito_p{ex.jito_tip_percentile}")
        return FeeDecision(level, cap, 0, urgent, "urgent_cap" if urgent else "size_cap")

    def modeled_fee_lamports(
        self, decision: FeeDecision, notional_usd: float | None = None, sol_price: float | None = None
    ) -> int:
        """What a transaction with this decision is expected to pay (paper trading)."""
        base = base_fee_lamports(self._config())
        if decision.jito_tip_lamports > 0:
            return base + decision.jito_tip_lamports  # a tip is paid exactly
        # Jupiter pays its estimate for the level, at most the cap
        expected = self.expected_swap_fee_lamports(None if decision.urgent else notional_usd, sol_price)
        return min(expected, base + decision.priority_max_lamports)
