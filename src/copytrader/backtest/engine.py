"""Walk-forward backtester.

For each window ``[t, t + test_days)``:

1. **train**: analyse, detect, score and select wallets using ONLY swaps with
   ``block_time < t`` (and ≥ ``t − train_days``) — no look-ahead;
2. **test**: replay the selected wallets' swaps inside the window as copy
   signals, with latency, slippage, price impact, fees, sizing and risk caps;
3. roll forward.

Positions opened in a window keep being managed (their source's sells are
still mirrored) after the wallet is deselected, like in production.

Baselines, simulated with the same execution model, keep the result honest:
* ``copy_all`` — copy every non-blacklisted wallet (no selection);
* ``top_pnl``  — naive selection by highest realized PnL in the train window.

Known biases (reported with the results):
* the wallet universe is the one chosen *today* (survivorship/selection bias);
* token liquidity/risk at trade time is only known for swaps captured live,
  so historical filters are approximations.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from copytrader.analysis import stats
from copytrader.analysis.analyzer import PriceAt, TokenContext, WalletAnalyzer
from copytrader.analysis.replication import ReplicationParams, build_params
from copytrader.config.models import AppConfig
from copytrader.core.models import SwapEvent
from copytrader.core.types import ExitMode, ListType, Side
from copytrader.detection.detector import SuspicionDetector
from copytrader.detection.rules import CoordinationIndex, DetectionContext
from copytrader.execution.costs import entry_rent_lamports, lamports_to_usd, swap_fee_lamports
from copytrader.positions.exits import PositionView, evaluate_exit, source_sell_fraction
from copytrader.risk.sizing import SizingInput, compute_size
from copytrader.scoring.scorer import ScoringEngine
from copytrader.scoring.status import decide_status
from copytrader.selection.selector import Candidate, select_wallets

# Only used when no event carries a SOL price (network costs are denominated in SOL).
FALLBACK_SOL_PRICE_USD = 150.0


@dataclass
class BacktestParams:
    start: datetime | None = None
    end: datetime | None = None
    train_days: int = 30
    test_days: int = 7
    top_n: int = 10
    latency_seconds: float = 3.0
    entry_slippage_pct: float = 2.0
    exit_slippage_pct: float = 2.0
    fee_usd_per_trade: float | None = None  # None = network-cost model of execution.costs
    impact_coefficient: float = 1.0
    exit_mode: ExitMode = ExitMode.PROTECTED
    capital_usd: float = 1000.0
    price_step_minutes: float = 15.0

    @classmethod
    def from_config(cls, cfg: AppConfig, **overrides: Any) -> BacktestParams:
        b = cfg.backtest
        params = cls(
            train_days=b.train_days,
            test_days=b.test_days,
            top_n=cfg.selection.top_n,
            latency_seconds=b.latency_seconds,
            entry_slippage_pct=b.entry_slippage_pct,
            exit_slippage_pct=b.exit_slippage_pct,
            fee_usd_per_trade=b.fee_usd_per_trade,
            impact_coefficient=b.impact_coefficient,
            exit_mode=cfg.exits.default_mode,
            capital_usd=cfg.risk.capital_usd,
        )
        for k, v in overrides.items():
            if v is not None and hasattr(params, k):
                setattr(params, k, ExitMode(v) if k == "exit_mode" else v)
        return params


@dataclass
class _Pos:
    mint: str
    wallet: str
    qty: float
    cost: float
    entry_price: float
    peak: float
    opened_at: datetime
    at_risk: float
    tp_hit: list[int] = field(default_factory=list)


@dataclass
class _Book:
    capital: float
    cash: float
    positions: dict[str, _Pos] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    fees: float = 0.0
    curve: list[tuple[datetime, float]] = field(default_factory=list)

    def equity(self, mark: Callable[[str], float | None]) -> float:
        value = 0.0
        for p in self.positions.values():
            px = mark(p.mint)
            value += p.qty * px if px else p.cost
        return self.cash + value


class Backtester:
    def __init__(self, config: Callable[[], AppConfig], *, price_at: PriceAt | None = None) -> None:
        self._config = config
        self.price_at = price_at
        self.analyzer = WalletAnalyzer(config)
        self.detector = SuspicionDetector()
        self.scorer = ScoringEngine(config)

    # ----------------------------------------------------------- selection
    def _select(
        self,
        swaps: dict[str, list[SwapEvent]],
        lists: dict[str, ListType],
        t: datetime,
        train_days: int,
        tokens: dict[str, TokenContext],
        previous: set[str],
        top_n: int,
        replication: ReplicationParams | None = None,
    ) -> tuple[set[str], dict[str, float], dict[str, float]]:
        cfg = self._config()
        lo = t - timedelta(days=train_days)
        window = {w: [s for s in ss if lo <= s.block_time < t] for w, ss in swaps.items()}
        analyses = {
            w: self.analyzer.analyze(i, w, ss, now=t, tokens=tokens, current_prices={}, replication=replication)
            for i, (w, ss) in enumerate(window.items())
        }
        coordination = CoordinationIndex(
            ((w, s.token_mint, s.block_time) for w, ss in window.items() for s in ss if s.side is Side.BUY),
            cfg.detection.coordination_window_seconds,
        )
        cands: list[Candidate] = []
        scores: dict[str, float] = {}
        pnl: dict[str, float] = {}
        for i, (w, a) in enumerate(analyses.items()):
            flags = self.detector.detect(
                DetectionContext(a, tokens, coordination, cfg.detection, cfg.scoring.degradation, t)
            )
            sc = self.scorer.score(a, flags)
            st = decide_status(
                list_type=lists.get(w, ListType.NONE),
                score=sc.score,
                metrics=a.all,
                flags=flags,
                rules=cfg.status_rules,
            )
            scores[w] = sc.score
            pnl[w] = a.all.realized_pnl_usd
            cands.append(Candidate(i, w, sc.score, st.status, lists.get(w, ListType.NONE)))
        sel_cfg = cfg.selection.model_copy(update={"top_n": top_n})
        selection = select_wallets(cands, previous, sel_cfg)
        return selection.addresses, scores, pnl

    # ----------------------------------------------------------- simulation
    def _price(self, mint: str, ts: datetime, fallback: float | None) -> float | None:
        if self.price_at is not None:
            px = self.price_at(mint, ts)
            if px:
                return px
        return fallback

    def _simulate(
        self,
        book: _Book,
        events: list[tuple[SwapEvent, bool]],
        params: BacktestParams,
        scores: dict[str, float],
        t0: datetime,
        t1: datetime,
    ) -> None:
        cfg = self._config()
        lat = timedelta(seconds=params.latency_seconds)
        last_px: dict[str, float] = {}
        sol_px = next((ev.sol_price_usd for ev, _ in events if ev.sol_price_usd), FALLBACK_SOL_PRICE_USD)

        def fee(buy: bool) -> float:
            if params.fee_usd_per_trade is not None:
                return params.fee_usd_per_trade
            lamports = swap_fee_lamports(cfg) + (entry_rent_lamports(cfg) if buy else 0)
            return lamports_to_usd(lamports, sol_px)

        step = timedelta(minutes=params.price_step_minutes)
        next_tick = t0

        def mark(mint: str) -> float | None:
            return last_px.get(mint)

        def close(pos: _Pos, fraction: float, price: float, when: datetime, reason: str) -> None:
            qty = pos.qty * fraction
            exit_fee = fee(buy=False)
            proceeds = qty * price * (1 - params.exit_slippage_pct / 100) - exit_fee
            cost = pos.cost * fraction
            book.cash += proceeds
            book.fees += exit_fee
            pos.qty -= qty
            pos.cost -= cost
            book.trades.append(
                {
                    "mint": pos.mint,
                    "wallet": pos.wallet,
                    "closed_at": when.isoformat(),
                    "pnl_usd": proceeds - cost,
                    "return_pct": 100 * (proceeds / cost - 1) if cost else 0,
                    "reason": reason,
                }
            )
            if pos.qty <= 1e-12 or fraction >= 0.999:
                book.positions.pop(pos.mint, None)

        def tick_until(until: datetime) -> None:
            nonlocal next_tick
            if self.price_at is None or (params.exit_mode is ExitMode.MIRROR and not book.positions):
                next_tick = until
                return
            while next_tick <= until:
                for pos in list(book.positions.values()):
                    px = self._price(pos.mint, next_tick, None)
                    if not px:
                        continue
                    last_px[pos.mint] = px
                    pos.peak = max(pos.peak, px)
                    view = PositionView(pos.entry_price, pos.peak, pos.opened_at, params.exit_mode, tuple(pos.tp_hit))
                    d = evaluate_exit(view, px, next_tick, cfg.exits)
                    if d is not None:
                        if d.tp_level is not None:
                            pos.tp_hit.append(d.tp_level)
                        close(pos, d.fraction, px, next_tick, d.trigger)
                book.curve.append((next_tick, book.equity(mark)))
                next_tick += step

        for ev, copyable in events:
            if ev.sol_price_usd:
                sol_px = ev.sol_price_usd
            tick_until(ev.block_time)
            when = ev.block_time + lat
            base = self._price(ev.token_mint, when, ev.price_usd)
            if not base:
                continue
            last_px[ev.token_mint] = base
            if ev.side is Side.SELL:
                pos = book.positions.get(ev.token_mint)
                if pos is None or pos.wallet != ev.wallet:
                    continue
                fraction = source_sell_fraction(ev.sold_fraction, params.exit_mode, cfg.exits)
                if fraction:
                    close(pos, fraction, base, when, "source_sell")
                continue
            if not copyable or ev.token_mint in book.positions:
                continue
            equity = book.equity(mark)
            exposure = equity - book.cash
            if len(book.positions) >= cfg.risk.max_open_positions:
                continue
            stop = cfg.exits.emergency_stop_loss_pct if params.exit_mode is ExitMode.MIRROR else cfg.exits.stop_loss_pct
            sizing_capital = min(params.capital_usd, equity)
            size = compute_size(
                SizingInput(
                    sizing_capital_usd=sizing_capital,
                    max_risk_per_trade_pct=cfg.risk.max_risk_per_trade_pct,
                    stop_distance_pct=stop,
                    wallet_score=scores.get(ev.wallet),
                    min_score=cfg.selection.min_score,
                    hourly_volatility=None,
                    liquidity_usd=ev.liquidity_usd,
                    est_slippage_pct=None,
                    max_slippage_pct=cfg.risk.max_slippage_pct,
                    is_high_risk=False,
                    same_category_positions=0,
                    max_trade_usd=min(cfg.risk.max_trade_usd, params.capital_usd * 0.25),
                    min_trade_usd=cfg.risk.min_trade_usd,
                    hard_cap_usd=params.capital_usd * 0.25,
                    total_capacity_usd=max(
                        0.0, min(sizing_capital * cfg.risk.max_total_exposure_pct / 100 - exposure, book.cash)
                    ),
                    token_capacity_usd=sizing_capital * cfg.risk.max_token_exposure_pct / 100,
                    wallet_risk_capacity_usd=sizing_capital * cfg.risk.max_risk_per_wallet_pct / 100,
                    high_risk_capacity_usd=sizing_capital,
                ),
                cfg.sizing,
            )
            if size.rejected_reason:
                continue
            impact = 0.0
            if ev.liquidity_usd:
                impact = params.impact_coefficient * size.size_usd / (ev.liquidity_usd / 2 + size.size_usd)
            price = base * (1 + params.entry_slippage_pct / 100 + impact)
            entry_fee = fee(buy=True)
            book.cash -= size.size_usd + entry_fee
            book.fees += entry_fee
            book.positions[ev.token_mint] = _Pos(
                ev.token_mint,
                ev.wallet,
                size.size_usd / price,
                size.size_usd + entry_fee,
                price,
                price,
                when,
                size.size_usd * stop / 100,
            )
        tick_until(t1)
        book.curve.append((t1, book.equity(mark)))

    # ------------------------------------------------------------------ run
    def run(
        self,
        swaps: dict[str, list[SwapEvent]],
        params: BacktestParams,
        *,
        lists: dict[str, ListType] | None = None,
        tokens: dict[str, TokenContext] | None = None,
        labels: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        lists = lists or {}
        tokens = {
            m: TokenContext(mint=m, category=t.category, pair_created_at=t.pair_created_at)
            for m, t in (tokens or {}).items()
        }  # drop time-varying fields (no look-ahead)
        all_times = [s.block_time for ss in swaps.values() for s in ss]
        if not all_times:
            return {"error": "sin datos"}
        first, last = min(all_times), max(all_times)
        start = params.start or first + timedelta(days=params.train_days)
        sol_price = next((sw.sol_price_usd for ss in swaps.values() for sw in ss if sw.sol_price_usd), None)
        # Selection uses the same "what would copying it return" estimate as the live system.
        replication = build_params(
            self._config(), latency_seconds=params.latency_seconds, sol_price_usd=sol_price, latency_source="backtest"
        )
        end = params.end or last
        if start >= end:
            return {"error": "rango insuficiente: amplía el historial o reduce train_days"}
        ordered = sorted(((s, w) for w, ss in swaps.items() for s in ss), key=lambda x: x[0].block_time)
        strategies = {
            "strategy": _Book(params.capital_usd, params.capital_usd),
            "copy_all": _Book(params.capital_usd, params.capital_usd),
            "top_pnl": _Book(params.capital_usd, params.capital_usd),
        }
        windows: list[dict[str, Any]] = []
        previous: set[str] = set()
        t = start
        while t < end:
            t1 = min(end, t + timedelta(days=params.test_days))
            selected, scores, pnl = self._select(
                swaps, lists, t, params.train_days, tokens, previous, params.top_n, replication
            )
            previous = selected
            eligible = {w for w in swaps if lists.get(w) is not ListType.BLACKLIST}
            naive = set(sorted(eligible, key=lambda w: pnl.get(w, 0.0), reverse=True)[: params.top_n])
            window_events = [s for s, _ in ordered if t <= s.block_time < t1]
            for name, chosen in (("strategy", selected), ("copy_all", eligible), ("top_pnl", naive)):
                events = [(s, s.wallet in chosen) for s in window_events]
                self._simulate(strategies[name], events, params, scores, t, t1)
            windows.append(
                {
                    "start": t.isoformat(),
                    "end": t1.isoformat(),
                    "selected": [
                        {"wallet": w, "label": (labels or {}).get(w), "score": round(scores[w], 1)}
                        for w in sorted(selected, key=lambda x: -scores[x])
                    ],
                    "trades": sum(
                        1 for tr in strategies["strategy"].trades if t.isoformat() <= tr["closed_at"] < t1.isoformat()
                    ),
                }
            )
            t = t1
        return {
            "params": {
                k: (v.isoformat() if isinstance(v, datetime) else (v.value if isinstance(v, ExitMode) else v))
                for k, v in params.__dict__.items()
            },
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "results": {name: _summary(book, params.capital_usd) for name, book in strategies.items()},
            "windows": windows,
            "notes": [
                "Solo se evalúan periodos fuera de muestra (walk-forward): el scoring de cada ventana usa "
                "exclusivamente datos anteriores.",
                "Sesgo de supervivencia/selección: el universo de wallets es el elegido hoy.",
                "Liquidez/riesgo históricos solo se conocen para swaps capturados en vivo; los filtros "
                "históricos son aproximados.",
                "Resultados pasados no garantizan resultados futuros.",
            ],
        }


def _summary(book: _Book, capital: float) -> dict[str, Any]:
    trades = book.trades
    pnls = [t["pnl_usd"] for t in trades]
    curve = [v for _, v in book.curve] or [capital]
    final = curve[-1]
    dd, _ = stats.max_drawdown([capital, *curve])
    gp = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    by_reason: dict[str, int] = defaultdict(int)
    for t in trades:
        by_reason[t["reason"]] += 1
    step = max(1, len(book.curve) // 300)
    return {
        "final_equity_usd": round(final, 2),
        "roi_pct": round(100 * (final / capital - 1), 3),
        "max_drawdown_pct": round(100 * dd, 3),
        "n_trades": len(trades),
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else None,
        "profit_factor": round(gp / gl, 3) if gl > 0 else None,
        "fees_usd": round(book.fees, 2),
        "open_positions_at_end": len(book.positions),
        "exits_by_reason": dict(by_reason),
        "equity_curve": [{"ts": ts.isoformat(), "equity": round(v, 2)} for ts, v in book.curve[::step]],
    }
