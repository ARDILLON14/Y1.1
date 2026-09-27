"""Suspicious-behaviour detectors.

Each rule is a small pure function ``(DetectionContext) -> Flag | None``.
Messages are in Spanish because they are shown verbatim to the operator
("por qué esta wallet está en OBSERVAR/BLOQUEADA").

Severity semantics:
* INFO     — informative, no score impact;
* WARNING  — score penalty; may push the wallet to OBSERVE;
* CRITICAL — the wallet is BLOCKED (never copied) while the flag is active.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from copytrader.analysis import stats
from copytrader.analysis.analyzer import TokenContext, WalletAnalysis
from copytrader.config.models import DegradationSection, DetectionSection
from copytrader.core.models import Flag
from copytrader.core.types import Severity, Side


class CoordinationIndex:
    """Index of every tracked wallet's buys to find near-simultaneous entries."""

    def __init__(self, buys: Iterable[tuple[str, str, datetime]], window_seconds: float) -> None:
        self.window = timedelta(seconds=window_seconds)
        grouped: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
        for wallet, mint, ts in buys:
            grouped[mint].append((ts, wallet))
        self._times: dict[str, list[datetime]] = {}
        self._wallets: dict[str, list[str]] = {}
        for mint, items in grouped.items():
            items.sort()
            self._times[mint] = [t for t, _ in items]
            self._wallets[mint] = [w for _, w in items]

    def partners(self, wallet: str, mint: str, ts: datetime) -> set[str]:
        times = self._times.get(mint)
        if not times:
            return set()
        lo = bisect_left(times, ts - self.window)
        hi = bisect_right(times, ts + self.window)
        return {w for w in self._wallets[mint][lo:hi] if w != wallet}


@dataclass(slots=True)
class DetectionContext:
    analysis: WalletAnalysis
    tokens: dict[str, TokenContext]
    coordination: CoordinationIndex | None
    cfg: DetectionSection
    degradation: DegradationSection
    now: datetime


Rule = Callable[[DetectionContext], Flag | None]


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def wash_trading(ctx: DetectionContext) -> Flag | None:
    trades = ctx.analysis.recon.closed
    if len(trades) < ctx.cfg.min_trades_for_detection:
        return None
    suspicious = [t for t in trades if abs(t.return_frac) * 100 <= ctx.cfg.wash_max_abs_return_pct
                  and t.holding_seconds / 60 <= ctx.cfg.wash_max_hold_minutes]
    if len(suspicious) < ctx.cfg.wash_min_roundtrips:
        return None
    frac = len(suspicious) / len(trades)
    if frac < ctx.cfg.wash_fraction_warning:
        return None
    sev = Severity.CRITICAL if frac >= ctx.cfg.wash_fraction_critical else Severity.WARNING
    return Flag("WASH_TRADING", sev,
                f"{_pct(frac)} de sus operaciones son compra-venta rápida sin resultado (posible wash trading)",
                {"fraction": frac, "count": len(suspicious)})


def coordinated_trading(ctx: DetectionContext) -> Flag | None:
    if ctx.coordination is None:
        return None
    buys = [s for s in ctx.analysis.swaps if s.side is Side.BUY]
    if len(buys) < ctx.cfg.min_trades_for_detection:
        return None
    partners: Counter[str] = Counter()
    hits = 0
    for b in buys:
        others = ctx.coordination.partners(ctx.analysis.address, b.token_mint, b.block_time)
        if len(others) >= ctx.cfg.coordination_min_other_wallets:
            hits += 1
            partners.update(others)
    frac = hits / len(buys)
    if frac < ctx.cfg.coordination_fraction:
        return None
    sev = Severity.CRITICAL if frac >= 0.7 else Severity.WARNING
    top = [w for w, _ in partners.most_common(5)]
    return Flag("COORDINATED", sev,
                f"{_pct(frac)} de sus compras coinciden en segundos con otras {len(top)} wallets "
                "(posible grupo coordinado o bundle)", {"fraction": frac, "partners": top})


def sniper_entries(ctx: DetectionContext) -> Flag | None:
    buys = [s for s in ctx.analysis.swaps if s.side is Side.BUY]
    known = [(b, ctx.tokens[b.token_mint].pair_created_at) for b in buys
             if b.token_mint in ctx.tokens and ctx.tokens[b.token_mint].pair_created_at]
    if len(known) < ctx.cfg.min_trades_for_detection:
        return None
    window = ctx.cfg.sniper_window_seconds
    snipes = [b for b, created in known if created and 0 <= (b.block_time - created).total_seconds() <= window]
    frac = len(snipes) / len(known)
    if frac < ctx.cfg.sniper_fraction:
        return None
    return Flag("SNIPER", Severity.WARNING,
                f"{_pct(frac)} de sus compras ocurren en los primeros {window:.0f}s del token "
                "(no replicable con latencia normal; posible insider)", {"fraction": frac, "count": len(snipes)})


def low_liquidity(ctx: DetectionContext) -> Flag | None:
    buys = [s for s in ctx.analysis.swaps if s.side is Side.BUY and s.value_usd]
    total = sum(b.value_usd or 0 for b in buys)
    if len(buys) < ctx.cfg.min_trades_for_detection or total <= 0:
        return None
    low = 0.0
    for b in buys:
        liq = b.liquidity_usd if b.liquidity_usd is not None else (
            ctx.tokens[b.token_mint].liquidity_usd if b.token_mint in ctx.tokens else None)
        if liq is not None and liq < ctx.cfg.low_liquidity_usd:
            low += b.value_usd or 0
    frac = low / total
    if frac < ctx.cfg.low_liquidity_fraction:
        return None
    return Flag("LOW_LIQUIDITY", Severity.WARNING,
                f"{_pct(frac)} de su volumen es en tokens con liquidez < ${ctx.cfg.low_liquidity_usd:,.0f}",
                {"fraction": frac})


def unreplicable(ctx: DetectionContext) -> Flag | None:
    trades = ctx.analysis.recon.closed
    if len(trades) < ctx.cfg.min_trades_for_detection:
        return None
    fast = [t for t in trades if t.holding_seconds < ctx.cfg.unreplicable_hold_seconds]
    frac = len(fast) / len(trades)
    if frac < ctx.cfg.unreplicable_fraction:
        return None
    sev = Severity.CRITICAL if frac >= 0.7 else Severity.WARNING
    return Flag("UNREPLICABLE", sev,
                f"{_pct(frac)} de sus operaciones duran menos de {ctx.cfg.unreplicable_hold_seconds:.0f}s "
                "(imposibles de copiar a tiempo)", {"fraction": frac})


def single_trade_dependence(ctx: DetectionContext) -> Flag | None:
    m = ctx.analysis.all
    share = m.concentration.get("top_trade_share")
    if share is None or m.n_closed_trades < ctx.cfg.min_trades_for_detection or m.realized_pnl_usd <= 0:
        return None
    without = m.outliers.get("pnl_without_top_trade") or 0.0
    if share >= ctx.cfg.single_trade_share_critical and without <= 0:
        return Flag("SINGLE_TRADE", Severity.CRITICAL,
                    f"Todo su beneficio depende de una sola operación ({_pct(share)}); sin ella pierde "
                    f"${-without:,.0f}", {"top_trade_share": share, "pnl_without_top": without})
    if share >= ctx.cfg.single_trade_share_warning:
        return Flag("SINGLE_TRADE", Severity.WARNING,
                    f"Una sola operación aporta el {_pct(share)} de su beneficio",
                    {"top_trade_share": share, "pnl_without_top": without})
    return None


def outlier_gains(ctx: DetectionContext) -> Flag | None:
    m = ctx.analysis.all
    share = m.outliers.get("outlier_pnl_share")
    if share is None or share < ctx.cfg.outlier_pnl_share or m.realized_pnl_usd <= 0:
        return None
    return Flag("OUTLIER_GAINS", Severity.WARNING,
                f"{_pct(share)} del beneficio viene de operaciones ≥{ctx.cfg.outlier_return_multiple:.0f}x "
                "(movimientos excepcionales, difícilmente repetibles)", {"share": share})


def rug_exposure(ctx: DetectionContext) -> Flag | None:
    trades = ctx.analysis.recon.closed
    if len(trades) < ctx.cfg.min_trades_for_detection:
        return None
    rugged = [t for t in trades if t.token_mint in ctx.tokens and ctx.tokens[t.token_mint].is_rugged]
    frac = len(rugged) / len(trades)
    if frac < ctx.cfg.rug_fraction:
        return None
    return Flag("RUG_EXPOSURE", Severity.WARNING,
                f"{_pct(frac)} de sus operaciones son en tokens que terminaron en rug pull",
                {"fraction": frac, "count": len(rugged)})


def high_risk_tokens(ctx: DetectionContext) -> Flag | None:
    trades = ctx.analysis.recon.closed
    if len(trades) < ctx.cfg.min_trades_for_detection:
        return None
    risky = [t for t in trades if t.token_mint in ctx.tokens
             and (ctx.tokens[t.token_mint].risk_score or 0) >= ctx.cfg.high_risk_token_score]
    frac = len(risky) / len(trades)
    if frac < ctx.cfg.high_risk_fraction:
        return None
    return Flag("HIGH_RISK_TOKENS", Severity.WARNING,
                f"{_pct(frac)} de sus operaciones son en tokens con señales de riesgo elevado",
                {"fraction": frac})


def behavior_change(ctx: DetectionContext) -> Flag | None:
    recent, older = ctx.analysis.recent_trades, ctx.analysis.older_trades
    k = ctx.cfg.behavior_min_recent
    if len(recent) < k or len(older) < k:
        return None
    ratio = ctx.cfg.behavior_change_ratio
    changes: dict[str, float] = {}

    def check(name: str, a: float | None, b: float | None) -> None:
        if a and b and a > 0 and b > 0:
            r = b / a
            if r >= ratio or r <= 1 / ratio:
                changes[name] = r

    check("tamaño", stats.median([t.cost_usd for t in older]), stats.median([t.cost_usd for t in recent]))
    check("duración", stats.median([t.holding_seconds for t in older]),
          stats.median([t.holding_seconds for t in recent]))

    def freq(ts: list) -> float | None:
        span = (ts[-1].closed_at - ts[0].opened_at).total_seconds() / 86400
        return len(ts) / span if span > 0 else None

    check("frecuencia", freq(older), freq(recent))
    if not changes:
        return None
    desc = ", ".join(f"{k} ×{v:.1f}" for k, v in changes.items())
    return Flag("BEHAVIOR_CHANGE", Severity.WARNING, f"Cambio brusco de comportamiento reciente: {desc}",
                {"ratios": changes})


def degradation(ctx: DetectionContext) -> Flag | None:
    d = ctx.degradation
    recent, older = ctx.analysis.recent_trades, ctx.analysis.older_trades
    if len(recent) < d.min_recent_trades or len(older) < d.min_recent_trades:
        return None
    wr_old = sum(t.is_win for t in older) / len(older)
    wr_new = sum(t.is_win for t in recent) / len(recent)
    reasons: list[str] = []
    test = stats.two_proportion_z(sum(t.is_win for t in older), len(older),
                                  sum(t.is_win for t in recent), len(recent))
    if test and test[1] < d.p_value and wr_old - wr_new >= d.win_rate_drop:
        reasons.append(f"win rate {_pct(wr_old)} → {_pct(wr_new)} (p={test[1]:.3f})")

    def pf(ts: list) -> float | None:
        gp = sum(t.pnl_usd for t in ts if t.pnl_usd > 0)
        gl = -sum(t.pnl_usd for t in ts if t.pnl_usd < 0)
        return stats.safe_ratio(gp, gl)

    pf_old, pf_new = pf(older), pf(recent)
    if pf_old is not None and pf_new is not None and pf_old >= d.historical_profit_factor_min \
            and pf_new < d.recent_profit_factor_floor:
        reasons.append(f"profit factor {pf_old:.2f} → {pf_new:.2f}")
    if not reasons:
        return None
    return Flag("DEGRADATION", Severity.WARNING,
                f"Deterioro en sus últimas {len(recent)} operaciones: " + "; ".join(reasons),
                {"win_rate_old": wr_old, "win_rate_new": wr_new, "pf_old": pf_old, "pf_new": pf_new})


def hft(ctx: DetectionContext) -> Flag | None:
    tpd = ctx.analysis.all.trades_per_day
    if tpd is None or tpd < ctx.cfg.hft_trades_per_day:
        return None
    return Flag("HIGH_FREQUENCY", Severity.WARNING,
                f"{tpd:.0f} operaciones/día: comportamiento de bot, no replicable", {"trades_per_day": tpd})


def inactive(ctx: DetectionContext) -> Flag | None:
    days = ctx.analysis.all.days_since_last_trade
    if days is None or days < ctx.cfg.inactive_days:
        return None
    return Flag("INACTIVE", Severity.INFO, f"Sin actividad desde hace {days:.0f} días", {"days": days})


def copy_impact(ctx: DetectionContext) -> Flag | None:
    buys = [s for s in ctx.analysis.swaps if s.side is Side.BUY and s.value_usd]
    known = []
    for b in buys:
        liq = b.liquidity_usd if b.liquidity_usd is not None else (
            ctx.tokens[b.token_mint].liquidity_usd if b.token_mint in ctx.tokens else None)
        if liq:
            known.append((b.value_usd or 0) / liq)
    if len(known) < ctx.cfg.min_trades_for_detection:
        return None
    frac = sum(1 for r in known if r >= ctx.cfg.copy_impact_liquidity_fraction) / len(known)
    if frac < ctx.cfg.copy_impact_fraction:
        return None
    return Flag("PRICE_IMPACT", Severity.WARNING,
                f"En el {_pct(frac)} de sus compras mueve el precio (≥{_pct(ctx.cfg.copy_impact_liquidity_fraction)}"
                " de la liquidez): tu copia entraría a un precio notablemente peor", {"fraction": frac})


def incomplete_history(ctx: DetectionContext) -> Flag | None:
    r = ctx.analysis.recon
    if r.n_sells < ctx.cfg.min_trades_for_detection:
        return None
    frac = r.unmatched_sells / r.n_sells
    if frac < 0.5:
        return None
    return Flag("INCOMPLETE_HISTORY", Severity.INFO,
                f"{_pct(frac)} de sus ventas no tienen compra conocida en el historial analizado",
                {"fraction": frac})


ALL_RULES: tuple[Rule, ...] = (
    wash_trading, coordinated_trading, sniper_entries, low_liquidity, unreplicable, single_trade_dependence,
    outlier_gains, rug_exposure, high_risk_tokens, behavior_change, degradation, hft, inactive, copy_impact,
    incomplete_history,
)
