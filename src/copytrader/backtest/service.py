"""Load history from the database and run/persist a backtest."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import structlog

from copytrader.analysis.analyzer import TokenContext
from copytrader.backtest.engine import Backtester, BacktestParams
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
        bt = Backtester(c.get_cfg, price_at=price_at, liquidity_at=liquidity_at)
        result = await asyncio.to_thread(bt.run, dict(swaps), params, lists=lists, tokens=tokens, labels=labels)
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
                bt_v = Backtester(_constant(cfg_v), price_at=price_at, liquidity_at=liquidity_at)
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
