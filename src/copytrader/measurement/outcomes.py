"""What did the token do after each COPY decision — including the rejected ones?

Every decided COPY signal (executed, rejected, expired or failed) is enrolled
with a reference price: the market price at decision time (else the quote,
else the source's price). The price is then sampled at each configured horizon
(5 min, 1 h, 24 h by default) and stored as a return versus that reference.

Grouping rejected signals by the check that rejected them answers "does this
filter protect my capital or only cost me winners?", with data instead of
intuition. Returns are gross (no fees, no exit rules): they describe the token,
not a trade.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.errors import CopyTraderError
from copytrader.core.types import SignalAction, SignalStatus
from copytrader.db.base import Database
from copytrader.db.models import Signal, SignalOutcome
from copytrader.db.repositories._util import insert_many_ignore
from copytrader.observability import metrics
from copytrader.providers.interfaces import TokenInfoProvider

log = structlog.get_logger(__name__)

DECIDED = (
    SignalStatus.EXECUTED.value,
    SignalStatus.REJECTED.value,
    SignalStatus.EXPIRED.value,
    SignalStatus.FAILED.value,
)
ENROLL_BATCH = 500


def horizon_key(minutes: float) -> str:
    return f"{minutes:g}"


def reference_price(sig: Signal) -> float | None:
    prices = (sig.decision or {}).get("prices") or {}
    for key in ("theoretical_price_usd", "quote_price_usd", "signal_price_usd"):
        value = prices.get(key)
        if value:
            return float(value)
    return sig.source_price_usd or None


def failed_check(sig: Signal) -> tuple[str | None, str | None]:
    if sig.status == SignalStatus.EXECUTED.value:
        return None, None
    for check in (sig.decision or {}).get("checks") or []:
        if not check.get("passed") and check.get("critical", True):
            return str(check.get("name")), str(check.get("label"))[:120]
    return None, None


class OutcomeTracker:
    def __init__(
        self, *, db: Database, clock: Clock, config: Callable[[], AppConfig], tokens: TokenInfoProvider
    ) -> None:
        self.db = db
        self.clock = clock
        self._config = config
        self.tokens = tokens
        self._stopped = asyncio.Event()

    async def enroll(self, now: datetime) -> int:
        cfg = self._config().measurement
        oldest = now - timedelta(minutes=max(cfg.outcome_horizons_minutes)) - timedelta(hours=1)
        async with self.db.session() as s:
            stmt = (
                select(Signal)
                .outerjoin(SignalOutcome, SignalOutcome.signal_id == Signal.id)
                .where(
                    SignalOutcome.id.is_(None),
                    Signal.action == SignalAction.COPY.value,
                    Signal.status.in_(DECIDED),
                    Signal.created_at >= oldest,
                )
                .order_by(Signal.id)
                .limit(ENROLL_BATCH)
            )
            rows: list[dict[str, Any]] = []
            for sig in (await s.execute(stmt)).scalars().all():
                price = reference_price(sig)
                if not price:
                    continue
                check, label = failed_check(sig)
                rows.append(
                    {
                        "signal_id": sig.id,
                        "wallet_id": sig.wallet_id,
                        "token_mint": sig.token_mint,
                        "status": sig.status,
                        "mode": sig.mode,
                        "failed_check": check,
                        "failed_label": label,
                        "reference_price_usd": price,
                        "reference_at": sig.decided_at or sig.detected_at,
                        "returns": {},
                        "completed": False,
                        "updated_at": now,
                    }
                )
            return await insert_many_ignore(s, SignalOutcome, rows, ["signal_id"])

    async def sample(self, now: datetime) -> int:
        cfg = self._config().measurement
        interval = cfg.outcome_interval_seconds
        async with self.db.session() as s:
            pending = list(
                (await s.execute(select(SignalOutcome).where(SignalOutcome.completed.is_(False)))).scalars().all()
            )
            due: dict[int, list[float]] = {}
            for row in pending:
                done = row.returns or {}
                hs = [
                    h
                    for h in cfg.outcome_horizons_minutes
                    if horizon_key(h) not in done and row.reference_at + timedelta(minutes=h) <= now
                ]
                if hs:
                    due[row.id] = hs
            if not due:
                return 0
            mints = sorted({row.token_mint for row in pending if row.id in due})
            try:
                prices = await self.tokens.prices(mints, max_age_seconds=interval)
            except CopyTraderError as exc:
                log.warning("outcome_prices_failed", error=str(exc))
                prices = {}
            sampled = 0
            for row in pending:
                if row.id not in due:
                    continue
                returns = dict(row.returns or {})
                price = prices.get(row.token_mint)
                for h in due[row.id]:
                    late = (now - (row.reference_at + timedelta(minutes=h))).total_seconds()
                    tolerance = max(2 * interval, cfg.max_sample_lateness_fraction * h * 60)
                    if late > tolerance:
                        returns[horizon_key(h)] = None  # missed: sampling now would mislabel the horizon
                    elif price:
                        returns[horizon_key(h)] = price / row.reference_price_usd - 1
                        sampled += 1
                row.returns = returns  # reassign: JSON columns only track replacement
                row.completed = all(horizon_key(h) in returns for h in cfg.outcome_horizons_minutes)
                row.updated_at = now
        return sampled

    async def run_once(self, now: datetime | None = None) -> tuple[int, int]:
        now = now or self.clock.now()
        return await self.enroll(now), await self.sample(now)

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                if self._config().measurement.track_outcomes:
                    await self.run_once()
            except Exception:
                metrics.ERRORS.labels(component="outcomes").inc()
                log.exception("outcome_tracker_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._config().measurement.outcome_interval_seconds
                )

    async def stop(self) -> None:
        self._stopped.set()
