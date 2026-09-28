"""Evaluation cycle: analyse → detect → score → status → select → persist → notify.

Runs periodically (``analysis.recompute_interval_seconds``) and on demand.
All wallets are evaluated together because some detectors (coordination) and
the selection itself need the whole universe.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from statistics import median
from typing import Any

import structlog

from copytrader.analysis.analyzer import PriceAt, TokenContext, WalletAnalysis, WalletAnalyzer
from copytrader.analysis.regimes import RegimeClassifier
from copytrader.analysis.replication import ReplicationParams, build_params
from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.errors import CopyTraderError
from copytrader.core.events import EventBus, WalletStatusChanged
from copytrader.core.models import Flag, SwapEvent
from copytrader.core.types import ListType, Side, WalletStatus
from copytrader.db.base import Database
from copytrader.db.models import SelectionSnapshot, WalletScore
from copytrader.db.repositories import AnalyticsRepo, ExecutionRepo, TokenRepo, TransactionRepo, WalletRepo
from copytrader.detection.detector import SuspicionDetector
from copytrader.detection.rules import CoordinationIndex, DetectionContext
from copytrader.observability import metrics as prom
from copytrader.providers.interfaces import SolPriceHistory, TokenInfoProvider
from copytrader.scoring.scorer import ScoreResult, ScoringEngine
from copytrader.scoring.status import StatusDecision, decide_status
from copytrader.selection.selector import Candidate, SelectionResult, select_wallets

log = structlog.get_logger(__name__)

SelectionListener = Callable[[SelectionResult], Awaitable[None]]


@dataclass(slots=True)
class WalletEvaluation:
    wallet_id: int
    address: str
    label: str | None
    list_type: ListType
    analysis: WalletAnalysis
    flags: list[Flag]
    score: ScoreResult
    status: StatusDecision
    previous_status: str | None


@dataclass(slots=True)
class CycleReport:
    evaluated: int = 0
    selected: list[str] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    replication: dict[str, Any] = field(default_factory=dict)


class EvaluationCycle:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        config: Callable[[], AppConfig],
        bus: EventBus,
        tokens: TokenInfoProvider,
        sol_history: SolPriceHistory | None = None,
        price_at: PriceAt | None = None,
    ) -> None:
        self.db = db
        self.clock = clock
        self._config = config
        self.bus = bus
        self.tokens = tokens
        self.sol_history = sol_history
        self.price_at = price_at
        self.analyzer = WalletAnalyzer(config)
        self.detector = SuspicionDetector()
        self.scorer = ScoringEngine(config)
        self._listeners: list[SelectionListener] = []
        self._lock = asyncio.Lock()
        self.last_report: CycleReport | None = None

    def on_selection(self, listener: SelectionListener) -> None:
        self._listeners.append(listener)

    async def _regimes(self, start: Any, now: Any) -> RegimeClassifier | None:
        if self.sol_history is None:
            return None
        a = self._config().analysis
        try:
            series = await self.sol_history.sol_series(start - timedelta(days=2), now)
        except CopyTraderError as exc:
            log.warning("regime_series_failed", error=str(exc))
            return None
        return RegimeClassifier(
            series,
            trend_threshold_pct=a.regime_trend_threshold_pct,
            extreme_threshold_pct=a.regime_high_vol_threshold_pct,
        )

    async def replication_params(self) -> ReplicationParams:
        """Copy-replication assumptions: YOUR latency (measured when possible), size and costs."""
        cfg = self._config()
        a = cfg.analysis
        if a.replication_latency_seconds is not None:
            latency, source = a.replication_latency_seconds, "config"
        else:
            async with self.db.session() as s:
                samples = await ExecutionRepo(s).entry_latencies()
            if len(samples) >= a.replication_min_latency_samples:
                latency, source = median(samples), "measured"
            else:
                latency, source = cfg.backtest.latency_seconds, "default"
        try:
            sol_price = await self.tokens.sol_price()
        except CopyTraderError:
            sol_price = None
        return build_params(cfg, latency_seconds=latency, sol_price_usd=sol_price, latency_source=source)

    async def run(self) -> CycleReport:
        async with self._lock:
            return await self._run()

    async def _run(self) -> CycleReport:
        cfg = self._config()
        started = self.clock.monotonic()
        now = self.clock.now()
        since = now - timedelta(days=cfg.analysis.history_days)
        async with self.db.session() as s:
            wallets = list(await WalletRepo(s).list())
            rows = await TransactionRepo(s).all_swaps(since=since, wallet_ids=[w.id for w in wallets])
            mints = {sw.token_mint for _, sw in rows}
            token_rows = await TokenRepo(s).get_many(mints)
        by_wallet: dict[int, list[SwapEvent]] = defaultdict(list)
        for wid, sw in rows:
            by_wallet[wid].append(sw)
        contexts = {
            mint: TokenContext(
                mint=mint,
                category=t.category or "unknown",
                liquidity_usd=t.last_liquidity_usd,
                market_cap_usd=t.last_market_cap_usd,
                risk_score=t.risk_score,
                is_rugged=t.is_rugged,
                pair_created_at=t.pair_created_at,
            )
            for mint, t in token_rows.items()
        }
        regimes = await self._regimes(since, now)
        replication = await self.replication_params() if cfg.analysis.replication_enabled else None

        analyses: dict[int, WalletAnalysis] = {}
        open_mints: set[str] = set()
        for w in wallets:
            analyses[w.id] = self.analyzer.analyze(
                w.id,
                w.address,
                by_wallet.get(w.id, []),
                now=now,
                tokens=contexts,
                current_prices={},
                regimes=regimes,
                price_at=self.price_at,
                replication=replication,
            )
            open_mints.update(lot.token_mint for lot in analyses[w.id].recon.open_lots if not lot.stale)
        prices: dict[str, float] = {}
        if open_mints:
            try:
                prices = await self.tokens.prices(sorted(open_mints), max_age_seconds=60)
            except CopyTraderError as exc:
                log.warning("open_lot_prices_failed", error=str(exc))
        if prices:  # re-run cheaply with marks so unrealized PnL is populated
            for w in wallets:
                if any(lot.token_mint in prices for lot in analyses[w.id].recon.open_lots):
                    analyses[w.id] = self.analyzer.analyze(
                        w.id,
                        w.address,
                        by_wallet.get(w.id, []),
                        now=now,
                        tokens=contexts,
                        current_prices=prices,
                        regimes=regimes,
                        price_at=self.price_at,
                        replication=replication,
                    )

        coordination = CoordinationIndex(
            (
                (a.address, sw.token_mint, sw.block_time)
                for a in analyses.values()
                for sw in a.swaps
                if sw.side is Side.BUY
            ),
            cfg.detection.coordination_window_seconds,
        )

        evaluations: list[WalletEvaluation] = []
        for w in wallets:
            analysis = analyses[w.id]
            ctx = DetectionContext(
                analysis=analysis,
                tokens=contexts,
                coordination=coordination,
                cfg=cfg.detection,
                degradation=cfg.scoring.degradation,
                now=now,
            )
            flags = self.detector.detect(ctx)
            score = self.scorer.score(analysis, flags)
            list_type = ListType(w.list_type)
            status = decide_status(
                list_type=list_type, score=score.score, metrics=analysis.all, flags=flags, rules=cfg.status_rules
            )
            evaluations.append(
                WalletEvaluation(w.id, w.address, w.label, list_type, analysis, flags, score, status, w.status)
            )

        previous = {w.address for w in wallets if w.selected}
        selection = select_wallets(
            [
                Candidate(e.wallet_id, e.address, e.score.score, e.status.status, e.list_type, e.label)
                for e in evaluations
            ],
            previous,
            cfg.selection,
        )
        await self._persist(evaluations, selection, now)
        await self._notify(evaluations)
        for listener in self._listeners:
            try:
                await listener(selection)
            except Exception:
                log.exception("selection_listener_failed")

        report = CycleReport(
            evaluated=len(evaluations),
            selected=sorted(selection.addresses),
            replication=replication.describe() if replication else {},
        )
        for e in evaluations:
            report.status_counts[e.status.status.value] = report.status_counts.get(e.status.status.value, 0) + 1
        for st in WalletStatus:
            prom.WALLETS.labels(status=st.value).set(report.status_counts.get(st.value, 0))
        prom.SELECTED_WALLETS.set(len(selection.selected))
        report.duration_seconds = round(self.clock.monotonic() - started, 3)
        self.last_report = report
        log.info(
            "evaluation_cycle_done",
            wallets=report.evaluated,
            selected=len(report.selected),
            statuses=report.status_counts,
            seconds=report.duration_seconds,
        )
        return report

    async def _persist(self, evaluations: list[WalletEvaluation], selection: SelectionResult, now: Any) -> None:
        cfg = self._config()
        selected = selection.addresses
        async with self.db.session() as s:
            wallets = WalletRepo(s)
            analytics = AnalyticsRepo(s)
            for e in evaluations:
                a = e.analysis
                for window, m in (("all", a.all), ("decayed", a.decayed), ("recent", a.recent)):
                    data = m.to_dict()
                    if window != "all":  # keep the secondary windows compact
                        for heavy in ("period_pnl", "by_token", "by_category", "by_holding", "by_regime"):
                            data.pop(heavy, None)
                    await analytics.upsert_metrics(e.wallet_id, window, data, now, cfg.analysis.metrics_snapshot_hours)
                await analytics.replace_flags(e.wallet_id, e.flags, now)
                is_selected = e.address in selected
                rank = selection.ranks.get(e.address)
                await analytics.add_score_throttled(
                    WalletScore(
                        wallet_id=e.wallet_id,
                        computed_at=now,
                        score=e.score.score,
                        score_hist=e.score.score_hist,
                        score_recent=e.score.score_recent,
                        confidence=e.score.confidence,
                        components=e.score.components,
                        penalties=e.score.penalties,
                        status=e.status.status.value,
                        status_reasons=e.status.reasons,
                        rank=rank,
                        selected=is_selected,
                    ),
                    cfg.analysis.score_snapshot_minutes,
                )
                w = await wallets.get(e.wallet_id)
                if w is None:
                    continue
                if w.status != e.status.status.value:
                    w.status_changed_at = now
                w.status = e.status.status.value
                w.status_reasons = e.status.reasons + (
                    [selection.reasons[e.address]] if e.address in selection.reasons else []
                )
                w.score = e.score.score
                w.rank = rank
                w.selected = is_selected
                w.analyzed_at = now
            await analytics.add_selection(
                SelectionSnapshot(
                    computed_at=now,
                    top_n=cfg.selection.top_n,
                    selected=sorted(selected),
                    details={a: r for a, r in selection.reasons.items() if a in selected},
                )
            )

    async def _notify(self, evaluations: list[WalletEvaluation]) -> None:
        for e in evaluations:
            new = e.status.status.value
            if e.previous_status is None or e.previous_status == new:
                continue
            degraded = e.previous_status == WalletStatus.ACTIVE.value and new != WalletStatus.ACTIVE.value
            self.bus.publish(
                WalletStatusChanged(
                    wallet=e.address,
                    wallet_label=e.label,
                    old_status=e.previous_status,
                    new_status=new,
                    reasons=e.status.reasons,
                    degraded=degraded,
                )
            )
