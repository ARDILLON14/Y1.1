"""Copy pipeline: never copy blindly.

For every BUY of a selected wallet:

 1. signal detected (latency measured)          7. current exposure / existing position
 2. token identified (metadata, category)       8. risk limits (risk engine, independent)
 3. liquidity                                   9. position size (risk engine sizing)
 4. price (fresh market data)                  10. still makes sense after the delay?
 5. estimated slippage for OUR size (quote)         (age, TTL, price deviation, re-quote)
 6. token risk (rug, authorities, extensions)  11. execute or reject
                                               12. record everything (checks + explanation)

The pipeline stops at the first failed check (no pointless API calls) and
stores every evaluated check with its value, limit and message, so the
dashboard and notifications can say *exactly* why a trade was or wasn't copied.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, NoReturn

import structlog

from copytrader.config.models import AppConfig
from copytrader.core import ids
from copytrader.core.clock import Clock
from copytrader.core.concurrency import KeyedLocks
from copytrader.core.errors import CopyTraderError
from copytrader.core.events import EventBus, SignalDecided
from copytrader.core.models import CheckResult, Decision, OrderRequest, Quote, TokenInfo
from copytrader.core.types import (
    LABELS_ES,
    ExitMode,
    ListType,
    OrderPurpose,
    Side,
    SignalStatus,
    TradeMode,
)
from copytrader.db.base import Database
from copytrader.db.repositories import EventLogRepo, PositionRepo, SignalRepo
from copytrader.execution.base import quote_price_usd
from copytrader.execution.costs import lamports_to_usd, round_trip_cost_lamports
from copytrader.execution.mode import ModeController
from copytrader.execution.service import ExecutionService
from copytrader.observability import metrics
from copytrader.risk.engine import EntryRequest, RiskEngine
from copytrader.signals.engine import SignalContext, SignalEngine, copyable, signal_age_limit

log = structlog.get_logger(__name__)
_COST_LABEL = "Coste de red de ida y vuelta asumible"


class Rejected(Exception):
    def __init__(self, status: SignalStatus = SignalStatus.REJECTED) -> None:
        super().__init__()
        self.status = status


class CopyPipeline:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        config: Callable[[], AppConfig],
        bus: EventBus,
        mode: ModeController,
        risk: RiskEngine,
        execution: ExecutionService,
        tokens: Any,
        signals: SignalEngine,
        token_locks: KeyedLocks,
    ) -> None:
        self.db = db
        self.clock = clock
        self._config = config
        self.bus = bus
        self.mode = mode
        self.risk = risk
        self.execution = execution
        self.tokens = tokens
        self.signals = signals
        self.locks = token_locks

    # ------------------------------------------------------------------ entry
    async def process_entry(self, ctx: SignalContext) -> None:
        started = time.perf_counter()
        checks: list[CheckResult] = []
        state: dict[str, Any] = {
            "prices": {"signal_price_usd": ctx.swap.price_usd},
            "size": None,
            "sizing": {},
            "token": None,
            "mode": self.mode.trade_mode,
            "fill": None,
        }
        status = SignalStatus.REJECTED
        try:
            async with self.locks.hold(ctx.swap.token_mint):
                status = await self._run(ctx, checks, state)
        except Rejected as rej:
            status = rej.status
        except Exception as exc:
            log.exception("pipeline_error")
            metrics.ERRORS.labels(component="pipeline").inc()
            checks.append(CheckResult("internal", "Error interno", False, message=type(exc).__name__))
            status = SignalStatus.FAILED
        await self._record(ctx, checks, state, status, started)

    def _check(
        self, checks: list[CheckResult], result: CheckResult, status_on_fail: SignalStatus = SignalStatus.REJECTED
    ) -> None:
        checks.append(result)
        if not result.passed and result.critical:
            raise Rejected(status_on_fail)

    @staticmethod
    def _fail(checks: list[CheckResult], result: CheckResult) -> NoReturn:
        checks.append(result)
        raise Rejected(SignalStatus.REJECTED)

    async def _run(self, ctx: SignalContext, checks: list[CheckResult], state: dict[str, Any]) -> SignalStatus:
        cfg = self._config()
        swap = ctx.swap
        now = self.clock.now()
        trade_mode: TradeMode | None = state["mode"]
        self._check(
            checks,
            CheckResult(
                "level",
                "Nivel operativo permite ejecutar",
                trade_mode is not None,
                int(self.mode.level),
                message=f"nivel {int(self.mode.level)}, modo "
                f"{trade_mode.value.upper() if trade_mode else 'sin ejecución'}",
            ),
        )
        assert trade_mode is not None

        # 1. wallet eligibility (fresh state, the selection may have changed since detection)
        info = self.signals.wallet(swap.wallet) or ctx.wallet
        eligible = copyable(info)
        self._check(
            checks,
            CheckResult(
                "wallet_eligible",
                "Wallet elegible",
                eligible,
                message=f"{LABELS_ES.get(info.status.value, info.status.value)}"
                f"{', seleccionada' if info.selected else ', no seleccionada'}",
            ),
        )
        min_score = (
            cfg.selection.whitelist_min_score if info.list_type is ListType.WHITELIST else cfg.selection.min_score
        )
        self._check(
            checks,
            CheckResult(
                "min_score",
                "Score mínimo superado",
                (info.score or 0) >= min_score,
                info.score,
                min_score,
                f"{info.score or 0:.1f} ≥ {min_score:.0f}",
            ),
        )

        # 10a. delay (cheap check first), adapted to how long this wallet holds its trades
        age = (now - swap.block_time).total_seconds()
        max_age, why = signal_age_limit(cfg, info.median_hold_seconds)
        self._check(
            checks,
            CheckResult(
                "signal_age",
                "Retraso de la señal aceptable",
                age <= max_age,
                round(age, 2),
                round(max_age, 2),
                f"{age:.1f}s (máx {max_age:.1f}s: {why})",
            ),
            SignalStatus.EXPIRED,
        )

        # 2. token identification + fresh market data
        try:
            token: TokenInfo = await self.tokens.get(swap.token_mint, max_age_seconds=cfg.risk.max_data_age_seconds)
        except CopyTraderError as exc:
            self._fail(checks, CheckResult("token_data", "Datos del token disponibles", False, message=str(exc)))
        state["token"] = token
        data_age = (self.clock.now() - token.fetched_at).total_seconds()
        self._check(
            checks,
            CheckResult(
                "token_data",
                "Datos de mercado recientes",
                token.price_usd is not None and data_age <= cfg.risk.max_data_age_seconds,
                round(data_age, 1),
                cfg.risk.max_data_age_seconds,
                f"{token.symbol or swap.token_mint[:6]}: datos de hace {data_age:.0f}s",
            ),
        )
        allowed, why = self._token_allowed(swap.token_mint)
        self._check(checks, CheckResult("token_allowed", "Token permitido", allowed, message=why))
        self._check(checks, CheckResult("decimals", "Decimales del token conocidos", token.decimals is not None))

        # 3. liquidity / market cap / age
        liq = token.liquidity_usd
        self._check(
            checks,
            CheckResult(
                "liquidity",
                "Liquidez suficiente",
                liq is not None and liq >= cfg.risk.min_liquidity_usd,
                liq,
                cfg.risk.min_liquidity_usd,
                f"${liq or 0:,.0f} (mín ${cfg.risk.min_liquidity_usd:,.0f})",
            ),
        )
        mcap = token.market_cap_usd
        self._check(
            checks,
            CheckResult(
                "market_cap",
                "Market cap en rango",
                mcap is not None and cfg.risk.min_market_cap_usd <= mcap <= cfg.risk.max_market_cap_usd,
                mcap,
                f"{cfg.risk.min_market_cap_usd:,.0f}-{cfg.risk.max_market_cap_usd:,.0f}",
                f"${mcap or 0:,.0f} (rango ${cfg.risk.min_market_cap_usd:,.0f} – ${cfg.risk.max_market_cap_usd:,.0f})",
            ),
        )
        age_min = token.age_minutes(now)
        self._check(
            checks,
            CheckResult(
                "token_age",
                "Antigüedad del token suficiente",
                age_min is not None and age_min >= cfg.risk.min_token_age_minutes,
                None if age_min is None else round(age_min, 1),
                cfg.risk.min_token_age_minutes,
                "desconocida" if age_min is None else f"{age_min:.0f} min",
            ),
        )

        # 6. token risk
        risk_ok, risk_msg = self._token_risk(token)
        self._check(
            checks,
            CheckResult(
                "token_risk",
                "Riesgo del token aceptable",
                risk_ok,
                token.risk_score,
                cfg.risk.max_token_risk_score,
                risk_msg,
            ),
        )
        is_high_risk = (token.risk_score or 0) >= cfg.risk.high_risk_score_threshold or (
            mcap is not None and mcap < cfg.risk.high_risk_max_market_cap_usd
        )

        # 7. existing position / cooldown
        async with self.db.session() as s:
            positions = PositionRepo(s)
            existing = await positions.open_for_token(trade_mode, swap.token_mint)
            last = await positions.last_closed_for_token(trade_mode, swap.token_mint)
        self._check(
            checks,
            CheckResult(
                "existing_position",
                "Sin posición previa en el token",
                existing is None or cfg.risk.allow_add_to_position,
                message=f"posición #{existing.id} abierta" if existing else "",
            ),
        )
        if last is not None and last.realized_pnl_usd < 0 and last.closed_at is not None:
            since = (now - last.closed_at).total_seconds() / 60
            self._check(
                checks,
                CheckResult(
                    "reentry_cooldown",
                    "Enfriamiento tras pérdida en el token",
                    since >= cfg.risk.reentry_cooldown_minutes,
                    round(since, 1),
                    cfg.risk.reentry_cooldown_minutes,
                    f"cerrada con pérdida hace {since:.0f} min",
                ),
            )

        # 4. price vs source price (before spending a quote)
        theoretical = token.price_usd
        state["prices"]["theoretical_price_usd"] = theoretical
        if swap.price_usd and theoretical:
            move = (theoretical / swap.price_usd - 1) * 100
            self._check(
                checks,
                CheckResult(
                    "market_move",
                    "Precio de mercado cerca del pagado por la wallet",
                    move <= cfg.latency.max_price_deviation_pct,
                    round(move, 3),
                    cfg.latency.max_price_deviation_pct,
                    f"{move:+.2f}% desde su compra (máx {cfg.latency.max_price_deviation_pct}%)",
                ),
                SignalStatus.EXPIRED,
            )

        # 8-9. risk engine (limits + sizing + reservation)
        exit_mode = info.exit_mode_override or cfg.exits.default_mode
        rd = await self.risk.evaluate_entry(
            EntryRequest(
                mode=trade_mode,
                token=token,
                source_wallet_id=info.id,
                wallet_score=info.score,
                exit_mode=exit_mode,
                is_high_risk=is_high_risk,
            )
        )
        state["sizing"] = rd.sizing
        checks.extend(rd.checks)
        if not rd.approved:
            raise Rejected()
        state["size"] = rd.size_usd
        try:
            # 8b. fixed network costs vs size: small trades rarely survive their own fees
            await self._check_round_trip_cost(rd.size_usd, checks)
            # 5. slippage for OUR size + 10b. deviation after the delay
            quote, _ = await self._quote_and_validate(ctx, token, rd.size_usd, checks, state)
            # 10c. TTL right before sending (+ re-quote if the quote got old)
            ttl_age = (self.clock.now() - ctx.detected_at).total_seconds()
            self._check(
                checks,
                CheckResult(
                    "signal_ttl",
                    "Señal todavía vigente (TTL)",
                    ttl_age <= cfg.latency.signal_ttl_seconds,
                    round(ttl_age, 2),
                    cfg.latency.signal_ttl_seconds,
                ),
                SignalStatus.EXPIRED,
            )
            if (
                cfg.latency.requote_before_execution
                and (self.clock.now() - quote.obtained_at).total_seconds() > cfg.latency.max_quote_age_seconds
            ):
                quote, _ = await self._quote_and_validate(ctx, token, rd.size_usd, checks, state, requote=True)
        except BaseException:
            self.risk.release(rd.reservation_id)
            raise

        # 11. execute
        req = OrderRequest(
            client_order_id=ids.entry_order_id(ctx.signal_key),
            purpose=OrderPurpose.ENTRY,
            side=Side.BUY,
            mode=trade_mode,
            token_mint=swap.token_mint,
            token_decimals=int(token.decimals or 0),
            input_mint=cfg.execution.quote_mint,
            output_mint=swap.token_mint,
            amount_in_raw=quote.in_amount_raw,
            slippage_bps=cfg.execution.slippage_bps,
            signal_id=ctx.signal_id,
            signal_price_usd=swap.price_usd,
            theoretical_price_usd=theoretical,
            notional_usd=rd.size_usd,
            max_price_deviation_pct=cfg.latency.max_price_deviation_pct,
            trace_id=ctx.trace_id,
        )
        order_ctx = {
            "source_wallet_id": info.id,
            "source_wallet": info.address,
            "exit_mode": exit_mode.value,
            "is_high_risk": is_high_risk,
            "category": token.category,
            "at_risk_usd": rd.at_risk_usd,
            "token_symbol": token.symbol,
            "token_mint": swap.token_mint,
            "decimals": token.decimals,
            "signal_price_usd": swap.price_usd,
            "theoretical_price_usd": theoretical,
            "trace_id": ctx.trace_id,
            "exit_params": {
                "stop_loss_pct": cfg.exits.stop_loss_pct,
                "emergency_stop_loss_pct": cfg.exits.emergency_stop_loss_pct,
            },
        }
        result = await self.execution.execute(req, quote=quote, context=order_ctx, reservation_id=rd.reservation_id)
        state["fill"] = result
        state["prices"]["fill_price_usd"] = result.fill_price_usd
        if result.success:
            checks.append(
                CheckResult(
                    "execution",
                    "Ejecución",
                    True,
                    round(result.value_usd, 2),
                    message=f"{trade_mode.value.upper()} ${result.value_usd:,.2f} a "
                    f"${result.fill_price_usd or 0:.8g}, slippage "
                    f"{(result.slippage_bps or 0) / 100:.2f}%",
                )
            )
            if result.latency_ms is not None:
                metrics.END_TO_END_LATENCY.labels(mode=trade_mode.value).observe(
                    max(0.0, (self.clock.now() - swap.block_time).total_seconds())
                )
            return SignalStatus.EXECUTED
        pending = result.error == "pending"
        checks.append(
            CheckResult(
                "execution",
                "Ejecución",
                False,
                message="pendiente de confirmación on-chain" if pending else (result.error or ""),
            )
        )
        return SignalStatus.APPROVED if pending else SignalStatus.FAILED

    async def _check_round_trip_cost(self, size_usd: float, checks: list[CheckResult]) -> None:
        cfg = self._config()
        limit = cfg.risk.max_round_trip_cost_pct
        sol_price = await self.tokens.sol_price()
        if not sol_price:
            no_price = CheckResult("round_trip_cost", _COST_LABEL, False, message="precio de SOL no disponible")
            self._fail(checks, no_price)
        cost_usd = lamports_to_usd(round_trip_cost_lamports(cfg), sol_price)
        pct = cost_usd / size_usd * 100 if size_usd > 0 else float("inf")
        self._check(
            checks,
            CheckResult(
                "round_trip_cost",
                _COST_LABEL,
                pct <= limit,
                round(pct, 2),
                limit,
                f"{cost_usd:.2f} USD = {pct:.2f}% del tamaño (máx {limit:g}%)",
            ),
        )

    async def _quote_and_validate(
        self,
        ctx: SignalContext,
        token: TokenInfo,
        size_usd: float,
        checks: list[CheckResult],
        state: dict[str, Any],
        requote: bool = False,
    ) -> tuple[Quote, float]:
        cfg = self._config()
        mode: TradeMode = state["mode"]
        sol_price = await self.tokens.sol_price()
        self._check(checks, CheckResult("sol_price", "Precio de SOL disponible", bool(sol_price)))
        assert sol_price
        lamports = int(size_usd / sol_price * 1e9)
        executor = self.execution.executor(mode)
        try:
            quote = await executor.quote(
                cfg.execution.quote_mint, ctx.swap.token_mint, lamports, cfg.execution.slippage_bps
            )
        except CopyTraderError as exc:
            self._fail(checks, CheckResult("quote", "Cotización disponible", False, message=str(exc)))
        req_like = OrderRequest(
            client_order_id="probe",
            purpose=OrderPurpose.ENTRY,
            side=Side.BUY,
            mode=mode,
            token_mint=ctx.swap.token_mint,
            token_decimals=int(token.decimals or 0),
            input_mint=cfg.execution.quote_mint,
            output_mint=ctx.swap.token_mint,
            amount_in_raw=lamports,
            slippage_bps=cfg.execution.slippage_bps,
        )
        price = quote_price_usd(req_like, quote, sol_price)
        theoretical = token.price_usd
        self._check(checks, CheckResult("quote", "Cotización disponible", price is not None, message=quote.route_label))
        assert price is not None
        state["prices"]["quote_price_usd"] = price
        suffix = " (re-cotización)" if requote else ""
        slip = max((price / theoretical - 1) * 100 if theoretical else 0.0, quote.price_impact_frac * 100)
        self._check(
            checks,
            CheckResult(
                "slippage",
                "Slippage estimado aceptable" + suffix,
                slip <= cfg.risk.max_slippage_pct,
                round(slip, 3),
                cfg.risk.max_slippage_pct,
                f"Estimated slippage = {slip:.2f}% · Maximum allowed = {cfg.risk.max_slippage_pct:.2f}%",
            ),
        )
        if ctx.swap.price_usd:
            dev = (price / ctx.swap.price_usd - 1) * 100
            self._check(
                checks,
                CheckResult(
                    "price_deviation",
                    "Desviación vs precio de la wallet" + suffix,
                    dev <= cfg.latency.max_price_deviation_pct,
                    round(dev, 3),
                    cfg.latency.max_price_deviation_pct,
                    f"mi precio {dev:+.2f}% vs el suyo (máx {cfg.latency.max_price_deviation_pct:.2f}%)",
                ),
                SignalStatus.EXPIRED,
            )
        return quote, price

    def _token_allowed(self, mint: str) -> tuple[bool, str]:
        r = self._config().risk
        if mint in self._config().providers.quote_mints:
            return False, "es un activo de cotización (SOL/stable)"
        if mint in r.token_blacklist:
            return False, "token en blacklist"
        if r.token_whitelist_only and mint not in r.token_whitelist:
            return False, "modo whitelist de tokens activo"
        return True, ""

    def _token_risk(self, token: TokenInfo) -> tuple[bool, str]:
        r = self._config().risk
        if token.is_rugged:
            return False, "token marcado como rug pull"
        if "mint" not in token.sources:
            return False, "no se pudo verificar el mint on-chain (autoridades desconocidas)"
        if r.block_mint_authority and token.mint_authority:
            return False, "mint authority activa (pueden emitir tokens)"
        if r.block_freeze_authority and token.freeze_authority:
            return False, "freeze authority activa (pueden congelar tu saldo)"
        if r.block_dangerous_extensions and token.dangerous_extensions:
            return False, f"extensiones peligrosas: {', '.join(token.dangerous_extensions)}"
        if self._config().providers.rugcheck.enabled and token.risk_score is None:
            return False, "informe de riesgo no disponible"
        if token.risk_score is not None and token.risk_score > r.max_token_risk_score:
            return False, f"score de riesgo {token.risk_score:.0f} > {r.max_token_risk_score:.0f}"
        return True, f"score {token.risk_score:.0f}" if token.risk_score is not None else "sin informe"

    # ----------------------------------------------------------------- record
    async def _record(
        self, ctx: SignalContext, checks: list[CheckResult], state: dict[str, Any], status: SignalStatus, started: float
    ) -> None:
        failed = [c for c in checks if not c.passed and c.critical]
        approved = status in (SignalStatus.EXECUTED, SignalStatus.APPROVED)
        reason = None
        if failed:
            f = failed[0]
            # Labels state the requirement ("Liquidez suficiente"): make the failure explicit.
            reason = f"No cumple — {f.label}: {f.message}" if f.message else f"No cumple — {f.label}"
        decision = Decision(
            approved=approved,
            checks=checks,
            reason=reason,
            size_usd=state["size"],
            sizing=state["sizing"],
            prices=state["prices"],
        )
        token: TokenInfo | None = state["token"]
        info = ctx.wallet
        header = (
            f"Wallet {info.label or info.address[:6] + '…' + info.address[-4:]}\n"
            f"Score: {info.score or 0:.0f}\n\n{'BUY' if ctx.swap.side is Side.BUY else 'SELL'} DETECTED"
        )
        explanation = decision.explain(header)
        mode: TradeMode | None = state["mode"]
        async with self.db.session() as s:
            sig = await SignalRepo(s).get(ctx.signal_id)
            if sig is not None:
                settled = sig.status in (SignalStatus.EXECUTED.value, SignalStatus.FAILED.value)
                if not (status is SignalStatus.APPROVED and settled):
                    # a pending order may already have been resolved by recovery: never downgrade it
                    sig.status = status.value
                    sig.reason = reason or ("Copiada" if status is SignalStatus.EXECUTED else sig.reason)
                sig.decision = decision.to_dict()
                sig.decided_at = self.clock.now()
                sig.mode = mode.value if mode else None
                if token is not None and token.symbol:
                    sig.token_symbol = token.symbol
            await EventLogRepo(s).add(
                "pipeline",
                "decision_approved" if approved else f"decision_{status.value}",
                level="info" if approved else "warning",
                trace_id=ctx.trace_id,
                data={
                    "signal_id": ctx.signal_id,
                    "reason": reason,
                    "size_usd": state["size"],
                    "mode": mode.value if mode else None,
                    "failed_check": failed[0].name if failed else None,
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
        metrics.PIPELINE_LATENCY.observe(time.perf_counter() - started)
        metrics.DECISIONS.labels(
            result="approved" if approved else status.value, reason=failed[0].name if failed else "ok"
        ).inc()
        log.info(
            "signal_decided",
            status=status.value,
            reason=reason,
            size_usd=state["size"],
            checks=len(checks),
            ms=round((time.perf_counter() - started) * 1000, 1),
        )
        fill = state["fill"]
        self.bus.publish(
            SignalDecided(
                trace_id=ctx.trace_id,
                signal_id=ctx.signal_id,
                wallet=info.address,
                wallet_label=info.label,
                wallet_score=info.score,
                token_mint=ctx.swap.token_mint,
                token_symbol=token.symbol if token else None,
                side=ctx.swap.side.value,
                action=ctx.action.value,
                approved=approved,
                reason=reason,
                explanation=explanation,
                source_price_usd=ctx.swap.price_usd,
                liquidity_usd=token.liquidity_usd if token else None,
                size_usd=fill.value_usd if fill and fill.success else state["size"],
                mode=mode.value if mode else None,
                checks=decision.to_dict()["checks"],
            )
        )


__all__ = ["CopyPipeline", "ExitMode"]
