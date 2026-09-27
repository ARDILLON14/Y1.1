"""Periodic risk supervision: equity snapshots, automatic kill switches, gauges."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import structlog

from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.types import TradeMode
from copytrader.db.base import Database
from copytrader.db.models import EquitySnapshot
from copytrader.db.repositories import EquityRepo, PositionRepo
from copytrader.execution.mode import ModeController
from copytrader.observability import metrics
from copytrader.risk.engine import BookState, RiskEngine

log = structlog.get_logger(__name__)


class RiskMonitor:
    def __init__(self, *, db: Database, clock: Clock, config: Callable[[], AppConfig], risk: RiskEngine,
                 mode: ModeController) -> None:
        self.db = db
        self.clock = clock
        self._config = config
        self.risk = risk
        self.mode = mode
        self._stopped = asyncio.Event()
        self.last_books: dict[TradeMode, BookState] = {}

    async def tick(self) -> None:
        active = self.mode.trade_mode
        async with self.db.session() as s:
            open_modes = {TradeMode(p.mode) for p in await PositionRepo(s).open_positions()}
        modes = set(open_modes)
        if active is not None:
            modes.add(active)
        for mode in sorted(modes, key=lambda m: m.value):
            book = (await self.risk.enforce_limits(mode) if mode is active
                    else await self.risk.book(mode, include_reservations=False))
            self.last_books[mode] = book
            async with self.db.session() as s:
                await EquityRepo(s).add(EquitySnapshot(
                    ts=self.clock.now(), mode=mode.value, equity_usd=book.equity_usd,
                    cash_usd=book.equity_usd - book.exposure_usd, exposure_usd=book.exposure_usd,
                    realized_pnl_usd=book.realized_total_usd, unrealized_pnl_usd=book.unrealized_usd,
                    drawdown_pct=book.drawdown_pct))
            metrics.EQUITY.labels(mode=mode.value).set(book.equity_usd)
            metrics.EXPOSURE.labels(mode=mode.value).set(book.exposure_usd)
            metrics.DAILY_PNL.labels(mode=mode.value).set(book.equity_usd - book.day_start_equity)

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick()
            except Exception:
                metrics.ERRORS.labels(component="risk_monitor").inc()
                log.exception("risk_monitor_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(),
                                       timeout=self._config().observability.equity_snapshot_seconds)

    async def stop(self) -> None:
        self._stopped.set()
