"""Load history from the database and run/persist a backtest."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import structlog

from copytrader.analysis.analyzer import TokenContext
from copytrader.backtest.engine import Backtester, BacktestParams
from copytrader.container import Container
from copytrader.core.types import ListType
from copytrader.db.repositories import BacktestRepo, TokenRepo, TransactionRepo, WalletRepo

log = structlog.get_logger(__name__)


async def run_backtest(c: Container, overrides: dict[str, Any] | None = None) -> int:
    params = BacktestParams.from_config(c.cfg, **(overrides or {}))
    async with c.db.session() as s:
        run = await BacktestRepo(s).create({k: (str(v) if v is not None else None) for k, v in params.__dict__.items()})
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
        bt = Backtester(c.get_cfg, price_at=market.token_price if market is not None else None)
        result = await asyncio.to_thread(bt.run, dict(swaps), params, lists=lists, tokens=tokens, labels=labels)
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
