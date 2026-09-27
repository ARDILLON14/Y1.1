"""Risk Management Engine.

Independent of copy trading: it knows nothing about wallets' strategies or
DEXes. It receives an ``EntryRequest`` and answers with a ``RiskDecision``
(checks with value/limit/message + size + a reservation).

Concurrency: ``evaluate_entry`` runs under a single lock and *reserves* the
approved exposure in memory until the fill is recorded (or the order fails),
so two simultaneous signals can never both consume the last slot of capacity.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.events import EventBus, RiskLimitHit
from copytrader.core.models import CheckResult, TokenInfo
from copytrader.core.types import ExitMode, KillSwitchScope, TradeMode
from copytrader.db.base import Database
from copytrader.db.models import Position
from copytrader.db.repositories import EquityRepo, OrderRepo, PositionRepo, RiskEventRepo
from copytrader.execution.mode import ModeController
from copytrader.observability import metrics
from copytrader.resilience.rate_limiter import SlidingWindowCounter
from copytrader.risk.killswitch import KillSwitchService
from copytrader.risk.limits import EffectiveLimits
from copytrader.risk.sizing import SizingInput, SizingResult, compute_size, estimate_slippage_pct

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Exposure:
    token_mint: str
    source_wallet_id: int | None
    notional_usd: float
    at_risk_usd: float
    is_high_risk: bool
    category: str | None
    position_id: int | None = None


@dataclass(slots=True)
class BookState:
    mode: TradeMode
    capital_usd: float
    realized_total_usd: float
    unrealized_usd: float
    exposures: list[Exposure]
    day_start_equity: float
    week_start_equity: float
    month_start_equity: float
    peak_equity: float
    consecutive_losses: int

    @property
    def equity_usd(self) -> float:
        return self.capital_usd + self.realized_total_usd + self.unrealized_usd

    @property
    def exposure_usd(self) -> float:
        return sum(e.notional_usd for e in self.exposures)

    def loss_pct(self, baseline: float) -> float:
        if baseline <= 0:
            return 0.0
        return max(0.0, 100 * (baseline - self.equity_usd) / baseline)

    @property
    def drawdown_pct(self) -> float:
        peak = max(self.peak_equity, self.equity_usd)
        return max(0.0, 100 * (peak - self.equity_usd) / peak) if peak > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value, "capital_usd": self.capital_usd, "equity_usd": round(self.equity_usd, 2),
            "realized_pnl_usd": round(self.realized_total_usd, 2), "unrealized_pnl_usd": round(self.unrealized_usd, 2),
            "exposure_usd": round(self.exposure_usd, 2), "open_positions": sum(
                1 for e in self.exposures if e.position_id is not None),
            "daily_loss_pct": round(self.loss_pct(self.day_start_equity), 3),
            "weekly_loss_pct": round(self.loss_pct(self.week_start_equity), 3),
            "monthly_loss_pct": round(self.loss_pct(self.month_start_equity), 3),
            "daily_pnl_usd": round(self.equity_usd - self.day_start_equity, 2),
            "drawdown_pct": round(self.drawdown_pct, 3), "consecutive_losses": self.consecutive_losses,
            "roi_pct": round(100 * (self.equity_usd - self.capital_usd) / self.capital_usd, 3)
            if self.capital_usd else None,
        }


@dataclass(slots=True)
class EntryRequest:
    mode: TradeMode
    token: TokenInfo
    source_wallet_id: int | None
    wallet_score: float | None
    exit_mode: ExitMode
    is_high_risk: bool


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    checks: list[CheckResult] = field(default_factory=list)
    size_usd: float = 0.0
    at_risk_usd: float = 0.0
    reservation_id: str | None = None
    sizing: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


def _period_starts(now: datetime) -> tuple[datetime, datetime, datetime]:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    return day, week, month


def position_notional(p: Position) -> float:
    if p.last_price_usd is not None and p.decimals is not None:
        return p.qty_raw / (10 ** p.decimals) * p.last_price_usd
    return p.cost_usd


class RiskEngine:
    def __init__(self, *, db: Database, config: Callable[[], AppConfig], clock: Clock, bus: EventBus,
                 kill: KillSwitchService, mode: ModeController) -> None:
        self.db = db
        self._config = config
        self.clock = clock
        self.bus = bus
        self.kill = kill
        self.mode = mode
        self._lock = asyncio.Lock()
        self._reservations: dict[str, tuple[TradeMode, Exposure]] = {}
        self._errors = SlidingWindowCounter(3600, clock=clock.monotonic)

    def limits(self) -> EffectiveLimits:
        return EffectiveLimits.from_config(self._config(), self.mode.level)

    def stop_distance_pct(self, exit_mode: ExitMode) -> float:
        ex = self._config().exits
        return ex.emergency_stop_loss_pct if exit_mode is ExitMode.MIRROR else ex.stop_loss_pct

    # ------------------------------------------------------------------ state
    async def book(self, mode: TradeMode, *, include_reservations: bool = True,
                   losses_since: datetime | None = None) -> BookState:
        cfg = self._config()
        now = self.clock.now()
        day, week, month = _period_starts(now)
        async with self.db.session() as s:
            positions = PositionRepo(s)
            open_rows = await positions.open_positions(mode)
            realized = await positions.realized_pnl_total(mode)
            recent_closed = await positions.last_closed(mode, limit=max(20, cfg.risk.max_consecutive_losses + 1))
            equity = EquityRepo(s)
            baselines: list[float | None] = []
            for start in (day, week, month):
                snap = await equity.last_before(mode, start) or await equity.first_since(mode, start)
                baselines.append(snap.equity_usd if snap else None)
            peak = await equity.peak(mode)
            in_flight = [o for o in await OrderRepo(s).in_flight(mode) if o.purpose == "entry"]
        exposures = [Exposure(token_mint=p.token_mint, source_wallet_id=p.source_wallet_id,
                              notional_usd=position_notional(p), at_risk_usd=p.at_risk_usd,
                              is_high_risk=p.is_high_risk, category=p.category, position_id=p.id)
                     for p in open_rows]
        unrealized = sum(position_notional(p) - p.cost_usd for p in open_rows)
        # Entry orders not yet filled (possibly from before a restart) also consume capacity.
        for o in in_flight:
            ctx = o.context or {}
            exposures.append(Exposure(token_mint=o.token_mint, source_wallet_id=ctx.get("source_wallet_id"),
                                      notional_usd=float(o.notional_usd or 0.0),
                                      at_risk_usd=float(ctx.get("at_risk_usd") or 0.0),
                                      is_high_risk=bool(ctx.get("is_high_risk")), category=ctx.get("category")))
        if include_reservations:
            exposures += [exp for m, exp in self._reservations.values() if m is mode]
        streak = 0
        for p in recent_closed:
            if losses_since is not None and p.closed_at is not None and p.closed_at <= losses_since:
                break
            if p.realized_pnl_usd < 0:
                streak += 1
            else:
                break
        state = BookState(mode=mode, capital_usd=cfg.risk.capital_usd, realized_total_usd=realized,
                          unrealized_usd=unrealized, exposures=exposures, day_start_equity=0.0,
                          week_start_equity=0.0, month_start_equity=0.0, peak_equity=peak or 0.0,
                          consecutive_losses=streak)
        current = state.equity_usd
        state.day_start_equity = baselines[0] or current
        state.week_start_equity = baselines[1] or current
        state.month_start_equity = baselines[2] or current
        state.peak_equity = max(state.peak_equity, current)
        return state

    # ------------------------------------------------------------ evaluation
    async def evaluate_entry(self, req: EntryRequest) -> RiskDecision:
        async with self._lock:
            return await self._evaluate(req)

    async def _evaluate(self, req: EntryRequest) -> RiskDecision:
        cfg = self._config()
        lim = self.limits()
        book = await self.book(req.mode)
        checks: list[CheckResult] = []

        ks = self.kill.blocking_reason()
        checks.append(CheckResult("kill_switch", "Kill switch inactivo", ks is None, message=ks or ""))
        for name, label, baseline, limit in (
            ("daily_loss", "Pérdida diaria dentro del límite", book.day_start_equity, lim.max_daily_loss_pct),
            ("weekly_loss", "Pérdida semanal dentro del límite", book.week_start_equity, lim.max_weekly_loss_pct),
            ("monthly_loss", "Pérdida mensual dentro del límite", book.month_start_equity,
             lim.max_monthly_loss_pct),
        ):
            loss = book.loss_pct(baseline)
            checks.append(CheckResult(name, label, loss < limit, round(loss, 3), limit,
                                      f"{loss:.2f}% (máx {limit:.2f}%)"))
        n_open = len(book.exposures)
        checks.append(CheckResult("open_positions", "Posiciones abiertas disponibles",
                                  n_open < lim.max_open_positions, n_open, lim.max_open_positions,
                                  f"{n_open}/{lim.max_open_positions}"))

        sizing_capital = max(0.0, min(lim.capital_usd, book.equity_usd))
        exposure = book.exposure_usd
        token_exp = sum(e.notional_usd for e in book.exposures if e.token_mint == req.token.mint)
        wallet_risk = sum(e.at_risk_usd for e in book.exposures
                          if req.source_wallet_id is not None and e.source_wallet_id == req.source_wallet_id)
        high_risk_exp = sum(e.notional_usd for e in book.exposures if e.is_high_risk)
        same_cat = sum(1 for e in book.exposures if e.category and e.category == req.token.category)
        cash_left = max(0.0, book.equity_usd - exposure)
        total_cap = max(0.0, min(sizing_capital * lim.max_total_exposure_pct / 100 - exposure, cash_left))
        token_cap = max(0.0, sizing_capital * lim.max_token_exposure_pct / 100 - token_exp)
        wallet_cap = max(0.0, sizing_capital * lim.max_risk_per_wallet_pct / 100 - wallet_risk)
        high_cap = max(0.0, sizing_capital * lim.max_high_risk_exposure_pct / 100 - high_risk_exp)
        stop = self.stop_distance_pct(req.exit_mode)
        probe = sizing_capital * lim.max_risk_per_trade_pct / max(stop, 1.0)
        sizing: SizingResult = compute_size(SizingInput(
            sizing_capital_usd=sizing_capital, max_risk_per_trade_pct=lim.max_risk_per_trade_pct,
            stop_distance_pct=stop, wallet_score=req.wallet_score, min_score=cfg.selection.min_score,
            hourly_volatility=req.token.hourly_volatility(), liquidity_usd=req.token.liquidity_usd,
            est_slippage_pct=estimate_slippage_pct(min(probe, lim.max_trade_usd), req.token.liquidity_usd),
            max_slippage_pct=lim.max_slippage_pct, is_high_risk=req.is_high_risk,
            same_category_positions=same_cat, max_trade_usd=lim.max_trade_usd, min_trade_usd=lim.min_trade_usd,
            hard_cap_usd=lim.hard_max_trade_usd, total_capacity_usd=total_cap, token_capacity_usd=token_cap,
            wallet_risk_capacity_usd=wallet_cap, high_risk_capacity_usd=high_cap), cfg.sizing)
        size = sizing.size_usd
        at_risk = size * stop / 100
        checks.append(CheckResult("position_size", "Tamaño de posición calculado", sizing.rejected_reason is None,
                                  round(size, 2), lim.min_trade_usd,
                                  sizing.rejected_reason or f"{size:.2f} USD (limitado por {sizing.limited_by})"))
        checks.append(CheckResult("total_exposure", "Exposición disponible", size <= total_cap + 1e-6,
                                  round(exposure + size, 2), round(sizing_capital * lim.max_total_exposure_pct / 100, 2),
                                  f"{exposure + size:.2f} / {sizing_capital * lim.max_total_exposure_pct / 100:.2f} USD"))
        checks.append(CheckResult("token_exposure", "Exposición al token dentro del límite",
                                  size <= token_cap + 1e-6, round(token_exp + size, 2),
                                  round(sizing_capital * lim.max_token_exposure_pct / 100, 2)))
        checks.append(CheckResult("wallet_risk", "Riesgo por wallet dentro del límite",
                                  at_risk <= wallet_cap + 1e-6, round(wallet_risk + at_risk, 2),
                                  round(sizing_capital * lim.max_risk_per_wallet_pct / 100, 2)))
        trade_risk_limit = sizing_capital * lim.max_risk_per_trade_pct / 100
        checks.append(CheckResult("trade_risk", "Riesgo por operación dentro del límite",
                                  at_risk <= trade_risk_limit * 1.001 + 1e-9, round(at_risk, 2),
                                  round(trade_risk_limit, 2), f"{at_risk:.2f} USD en riesgo si salta el stop"))
        if req.is_high_risk:
            checks.append(CheckResult("high_risk_exposure", "Exposición a alto riesgo dentro del límite",
                                      size <= high_cap + 1e-6, round(high_risk_exp + size, 2),
                                      round(sizing_capital * lim.max_high_risk_exposure_pct / 100, 2)))

        failed = [c for c in checks if not c.passed and c.critical]
        decision = RiskDecision(approved=not failed, checks=checks, size_usd=size if not failed else 0.0,
                                at_risk_usd=at_risk, sizing=sizing.to_dict(),
                                reason=failed[0].message or failed[0].label if failed else None)
        if decision.approved:
            rid = secrets.token_hex(8)
            self._reservations[rid] = (req.mode, Exposure(
                token_mint=req.token.mint, source_wallet_id=req.source_wallet_id, notional_usd=size,
                at_risk_usd=at_risk, is_high_risk=req.is_high_risk, category=req.token.category))
            decision.reservation_id = rid
        self._update_gauges(book, lim)
        return decision

    def release(self, reservation_id: str | None) -> None:
        if reservation_id:
            self._reservations.pop(reservation_id, None)

    def _update_gauges(self, book: BookState, lim: EffectiveLimits) -> None:
        cap = max(1e-9, lim.capital_usd)
        metrics.RISK_UTILIZATION.labels(limit="exposure").set(book.exposure_usd / (cap * lim.max_total_exposure_pct / 100))
        metrics.RISK_UTILIZATION.labels(limit="positions").set(len(book.exposures) / lim.max_open_positions)
        metrics.RISK_UTILIZATION.labels(limit="daily_loss").set(book.loss_pct(book.day_start_equity)
                                                                / lim.max_daily_loss_pct)

    # -------------------------------------------------------- automatic stops
    async def record_execution_error(self, detail: str) -> None:
        count = self._errors.add()
        limit = self._config().risk.max_execution_errors_per_hour
        if limit and count >= limit:
            await self.kill.activate(KillSwitchScope.GLOBAL,
                                     f"{count} errores de ejecución en la última hora (último: {detail[:120]})")

    async def enforce_limits(self, mode: TradeMode) -> BookState:
        """Called periodically: trip kill switches when loss limits are exceeded."""
        lim = self.limits()
        cfg = self._config()
        book = await self.book(mode, include_reservations=False,
                               losses_since=self.kill.last_reset_at(KillSwitchScope.GLOBAL))
        daily = book.loss_pct(book.day_start_equity)
        if daily >= lim.max_daily_loss_pct:
            if await self.kill.activate(KillSwitchScope.DAILY,
                                        f"Pérdida diaria {daily:.2f}% ≥ {lim.max_daily_loss_pct:.2f}%"):
                await self._risk_event("daily_loss", daily, lim.max_daily_loss_pct)
        for name, value, limit in (("weekly_loss", book.loss_pct(book.week_start_equity), lim.max_weekly_loss_pct),
                                   ("monthly_loss", book.loss_pct(book.month_start_equity),
                                    lim.max_monthly_loss_pct)):
            if value >= limit and await self.kill.activate(
                    KillSwitchScope.GLOBAL, f"Pérdida {name.split('_')[0]} {value:.2f}% ≥ {limit:.2f}%"):
                await self._risk_event(name, value, limit)
        n = cfg.risk.max_consecutive_losses
        if n and book.consecutive_losses >= n and await self.kill.activate(
                KillSwitchScope.GLOBAL, f"{book.consecutive_losses} pérdidas consecutivas"):
            await self._risk_event("consecutive_losses", book.consecutive_losses, n)
        return book

    async def _risk_event(self, name: str, value: float, limit: float) -> None:
        async with self.db.session() as s:
            await RiskEventRepo(s).add(name, "critical", f"{name}: {value:.2f} ≥ {limit:.2f}",
                                       {"value": value, "limit": limit})
        self.bus.publish(RiskLimitHit(limit=name, message=f"{name}: {value:.2f} ≥ {limit:.2f}",
                                      value=value, threshold=limit))
