from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from copytrader.config.loader import deep_merge
from copytrader.config.secrets import Secrets
from copytrader.config.service import ConfigService
from copytrader.container import Container
from copytrader.core.models import SwapEvent
from copytrader.core.types import Side, TxSource
from copytrader.db.base import Database
from copytrader.db.repositories import DbConfigStore

BASE_TEST_CONFIG: dict[str, Any] = {
    "app": {"operating_level": 3, "environment": "test"},
    "providers": {
        "mode": "simulated",
        "simulated": {"seed": 11, "n_wallets": 17, "n_tokens": 50, "history_days": 45, "realtime_trades_per_minute": 0},
        "token_categories_file": None,
    },
    "analysis": {"history_days": 45, "recompute_interval_seconds": 3600},
    "paper": {"simulated_latency_ms": 0},
    "observability": {"metrics_enabled": False, "json_logs": False},
    "api": {"enabled": False},
    "risk": {
        "min_liquidity_usd": 30000,
        "min_token_age_minutes": 60,
        "max_token_risk_score": 60,
        "reentry_cooldown_minutes": 0,
    },
    "latency": {"max_quote_age_seconds": 30},
}


async def make_container(tmp_path: Any, overrides: dict[str, Any] | None = None) -> Container:
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await db.create_all()
    raw = deep_merge(BASE_TEST_CONFIG, overrides or {})
    config = ConfigService(raw, DbConfigStore(db))
    secrets = Secrets(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db")  # type: ignore[call-arg]
    c = Container(config, secrets, db=db)
    await c.mode.load()
    await c.kill.load()
    return c


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: Any) -> Path:
    """Backfill + evaluate the simulated universe ONCE; tests start from a copy."""
    path = tmp_path_factory.mktemp("template")

    async def build() -> None:
        c = await make_container(path)
        await seed_and_evaluate(c)
        async with c.db.engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        await c.aclose()

    asyncio.run(build())
    return Path(path) / "test.db"


async def seeded_container(tmp_path: Any, template: Path, overrides: dict[str, Any] | None = None) -> Container:
    shutil.copy(template, Path(tmp_path) / "test.db")
    c = await make_container(tmp_path, overrides)
    await c.refresh_tracking()
    return c


@pytest.fixture
async def container(tmp_path: Any, template_db: Path) -> AsyncIterator[Container]:
    c = await seeded_container(tmp_path, template_db)
    yield c
    await c.signals.stop()
    await c.bus.drain(1)
    await c.aclose()


async def seed_and_evaluate(c: Container) -> None:
    market = c.providers.simulated_market
    for w in market.wallets.values():
        await c.collector.add_wallet(w.address, label=w.label)
    await c.collector.backfill_all()
    await c.cycle.run()
    await c.refresh_tracking()


def good_token(c: Container) -> str:
    """A simulated token that passes every entry filter right now."""
    import asyncio  # noqa: F401

    market = c.providers.simulated_market
    now = c.clock.now()
    cfg = c.cfg.risk
    for mint, tok in market.tokens.items():
        liq = market.liquidity(mint, now) or 0
        price = market.token_price(mint, now) or 0
        mcap = price * tok.supply
        if (
            liq >= cfg.min_liquidity_usd * 2
            and cfg.min_market_cap_usd <= mcap <= cfg.max_market_cap_usd
            and (now - tok.created_at).total_seconds() > 7200
            and tok.rug_at is None
            and tok.risk_score < 30
            and not tok.mint_authority
            and not tok.freeze_authority
            and not tok.dangerous
        ):
            return mint
    raise AssertionError("no suitable token in the simulated market")


def live_swap(
    c: Container,
    wallet: str,
    mint: str,
    side: Side,
    usd: float,
    *,
    qty: float | None = None,
    before: float | None = None,
    after: float | None = None,
    sig: str | None = None,
    age_seconds: float = 1.0,
) -> SwapEvent:
    from datetime import timedelta

    market = c.providers.simulated_market
    now = c.clock.now()
    price = market.token_price(mint, now)
    qty = qty if qty is not None else usd / price
    sol = market.sol_price(now)
    ev = SwapEvent(
        wallet=wallet,
        signature=sig or f"test-{side.value}-{mint[:6]}-{now.timestamp()}",
        slot=1,
        block_time=now - timedelta(seconds=age_seconds),
        token_mint=mint,
        side=side,
        token_amount=qty,
        token_decimals=6,
        quote_mint="So11111111111111111111111111111111111111112",
        quote_amount=usd / sol,
        price_quote=(usd / sol) / qty,
        price_usd=usd / qty,
        value_usd=usd,
        sol_price_usd=sol,
        token_balance_before=before,
        token_balance_after=after,
        source=TxSource.STREAM,
        detected_at=now,
    )
    return replace(ev)
