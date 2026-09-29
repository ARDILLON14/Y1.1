"""Load history from the database and run/persist a backtest."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import structlog

from copytrader.analysis.analyzer import TokenContext
from copytrader.backtest.engine import Backtester, BacktestParams
from copytrader.backtest.history import PriceHistory, load_price_history, price_needs
from copytrader.config.models import AppConfig
from copytrader.container import Container
from copytrader.core.errors import ConfigError
from copytrader.core.types import ListType
from copytrader.db.repositories import BacktestRepo, TokenRepo, TransactionRepo, WalletRepo

log = structlog.get_logger(__name__)


MAX_VARIANTS = 2
BASE_NAME = "Configuración actual"


Variant = tuple[str, dict[str, Any], AppConfig]


def validate_variants(c: Container, variants: list[dict[str, Any]] | None) -> list[Variant]:
    """Each variant is a runtime config patch, validated exactly like a live change (hard limits included)."""
    out: list[Variant] = []
    for i, v in enumerate(variants or []):
        name = str(v.get("name") or f"Variante {i + 1}")[:40]
        patch = v.get("patch") or {}
        if not isinstance(patch, dict) or not patch:
            raise ConfigError(f"la variante '{name}' no tiene cambios")
        out.append((name, patch, c.config_service.preview(patch)))
    if len(out) > MAX_VARIANTS:
        raise ConfigError(f"máximo {MAX_VARIANTS} variantes por backtest")
    return out


def _constant(cfg: AppConfig) -> Callable[[], AppConfig]:
    return lambda: cfg


async def _load_history(
    c: Container, swaps: dict[str, list[Any]], params: BacktestParams, run_id: int
) -> tuple[PriceHistory, dict[str, Any]]:
    """Real candles for the tokens traded in the evaluated period (cached; missing ones downloaded)."""
    b = c.cfg.backtest
    times = [s.block_time for ss in swaps.values() for s in ss]
    now = c.clock.now()
    if not times:
        return PriceHistory(b.candle_minutes), {}
    start = params.start or min(times) + timedelta(days=params.train_days)
    end = params.end or max(times)
    needs, total = price_needs(swaps, start, end, now, b.max_price_tokens)

    async def progress(done: int, todo: int) -> None:
        if done != todo and done % 5:
            return
        async with c.db.session() as s:
            row = await BacktestRepo(s).get(run_id)
            if row is not None:
                row.results = {"progress": f"Descargando precios históricos: {done}/{todo} tokens"}

    history = await load_price_history(
        c.db,
        c.price_history_client,
        needs,
        b.candle_minutes,
        now=now,
        refetch_failed_after=timedelta(hours=b.refetch_failed_after_hours),
        progress=progress,
    )
    info = {"source": "geckoterminal", "candle_minutes": b.candle_minutes, "tokens_traded": total, **history.stats}
    return history, info


def _prices_note(p: dict[str, Any]) -> str:
    covered, traded = p.get("tokens_with_prices", 0), p.get("tokens_traded", 0)
    note = (
        f"Precios históricos: velas de {p.get('candle_minutes')} min (GeckoTerminal) para {covered} de {traded} "
        "tokens comprados en el periodo. En ellos, stop loss, take profit, trailing y tiempo máximo se evalúan "
        "entre operaciones, suponiendo dentro de cada vela el peor orden (primero el stop). El resto solo "
        "usa los precios de las operaciones de las wallets."
    )
    if p.get("failed"):
        note += f" {p['failed']} descargas fallaron (se reintentarán en el próximo backtest)."
    return note


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    strategy = dict(result.get("results", {}).get("strategy") or {})
    strategy.pop("equity_curve", None)
    return strategy


async def run_backtest(
    c: Container, overrides: dict[str, Any] | None = None, variants: list[dict[str, Any]] | None = None
) -> int:
    variant_cfgs = validate_variants(c, variants)
    params = BacktestParams.from_config(c.cfg, **(overrides or {}))
    stored: dict[str, Any] = {k: (str(v) if v is not None else None) for k, v in params.__dict__.items()}
    if variant_cfgs:
        stored["variants"] = [{"name": name, "patch": patch} for name, patch, _ in variant_cfgs]
    async with c.db.session() as s:
        run = await BacktestRepo(s).create(stored)
        run_id = run.id
    try:
        async with c.db.session() as s:
            wallets = list(await WalletRepo(s).list())
            rows = await TransactionRepo(s).all_swaps(wallet_ids=[w.id for w in wallets])
            token_rows = await TokenRepo(s).get_many({sw.token_mint for _, sw in rows})
        by_id = {w.id: w for w in wallets}
        swaps: dict[str, list[Any]] = defaultdict(list)
        for wid, sw in rows:
            swaps[by_id[wid].address].append(sw)
        tokens = {
            m: TokenContext(mint=m, category=t.category, pair_created_at=t.pair_created_at)
            for m, t in token_rows.items()
        }
        lists = {w.address: ListType(w.list_type) for w in wallets}
        labels = {w.address: w.label for w in wallets}
        market = c.providers.simulated_market
        price_at = market.token_price if market is not None else None
        liquidity_at = market.liquidity if market is not None else None
        history: PriceHistory | None = None
        prices: dict[str, Any] | None = None
        if market is None and c.cfg.backtest.historical_prices:
            history, prices = await _load_history(c, swaps, params, run_id)
        bt = Backtester(c.get_cfg, price_at=price_at, liquidity_at=liquidity_at, history=history)
        result = await asyncio.to_thread(bt.run, dict(swaps), params, lists=lists, tokens=tokens, labels=labels)
        if prices is not None and "error" not in result:
            result["prices"] = prices
            result["notes"].insert(0, _prices_note(prices))
        if variant_cfgs and "error" not in result:
            compared: list[dict[str, Any]] = [
                {
                    "name": BASE_NAME,
                    "patch": {},
                    "summary": _summary(result),
                    "equity_curve": result["results"]["strategy"].get("equity_curve", []),
                }
            ]
            for name, patch, cfg_v in variant_cfgs:
                params_v = BacktestParams.from_config(cfg_v, **(overrides or {}))
                bt_v = Backtester(_constant(cfg_v), price_at=price_at, liquidity_at=liquidity_at, history=history)
                res_v = await asyncio.to_thread(
                    bt_v.run, dict(swaps), params_v, lists=lists, tokens=tokens, labels=labels
                )
                compared.append(
                    {
                        "name": name,
                        "patch": patch,
                        "summary": _summary(res_v),
                        "equity_curve": (res_v.get("results", {}).get("strategy") or {}).get("equity_curve", []),
                        "error": res_v.get("error"),
                    }
                )
            result["variants"] = compared
        status = "error" if "error" in result else "done"
        async with c.db.session() as s:
            row = await BacktestRepo(s).get(run_id)
            if row is not None:
                row.status = status
                row.results = result
                row.error = result.get("error")
    except Exception as exc:
        log.exception("backtest_failed", run_id=run_id)
        async with c.db.session() as s:
            row = await BacktestRepo(s).get(run_id)
            if row is not None:
                row.status = "error"
                row.error = f"{type(exc).__name__}: {exc}"
    return run_id
