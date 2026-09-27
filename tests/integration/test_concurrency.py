"""Race conditions: simultaneous signals must never exceed risk limits or duplicate trades."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from copytrader.core.types import PositionStatus, Side
from copytrader.db.models import Order, Position
from copytrader.db.repositories import WalletRepo
from tests.integration.conftest import live_swap, seeded_container


def good_tokens(c, n):
    market = c.providers.simulated_market
    now = c.clock.now()
    cfg = c.cfg.risk
    out = []
    for mint, tok in market.tokens.items():
        liq = market.liquidity(mint, now) or 0
        price = market.token_price(mint, now) or 0
        if (
            liq >= cfg.min_liquidity_usd * 2
            and cfg.min_market_cap_usd <= price * tok.supply <= cfg.max_market_cap_usd
            and (now - tok.created_at).total_seconds() > 7200
            and tok.rug_at is None
            and tok.risk_score < 30
            and not tok.mint_authority
            and not tok.freeze_authority
            and not tok.dangerous
        ):
            out.append(mint)
    return out[:n]


async def test_parallel_signals_respect_max_open_positions(tmp_path, template_db):
    c = await seeded_container(tmp_path, template_db, {"risk": {"max_open_positions": 2}, "signals": {"partitions": 8}})
    c.signals.start()
    async with c.db.session() as s:
        wallet = next(w for w in await WalletRepo(s).list() if w.selected)
    mints = good_tokens(c, 6)
    assert len(mints) >= 4
    swaps = [live_swap(c, wallet.address, m, Side.BUY, 700.0, sig=f"par-{i}") for i, m in enumerate(mints)]
    await asyncio.gather(*(c.signals.on_swap(sw) for sw in swaps))
    await c.signals.drain()
    async with c.db.session() as s:
        open_positions = (
            await s.execute(
                select(func.count()).select_from(Position).where(Position.status == PositionStatus.OPEN.value)
            )
        ).scalar_one()
    assert open_positions == 2
    await c.signals.stop()
    await c.aclose()


async def test_same_signal_delivered_concurrently_executes_once(tmp_path, template_db):
    c = await seeded_container(tmp_path, template_db)
    c.signals.start()
    async with c.db.session() as s:
        wallet = next(w for w in await WalletRepo(s).list() if w.selected)
    mint = good_tokens(c, 1)[0]
    sw = live_swap(c, wallet.address, mint, Side.BUY, 700.0, sig="dup-sig")
    await asyncio.gather(*(c.signals.on_swap(sw) for _ in range(5)))
    await c.signals.drain()
    async with c.db.session() as s:
        n_orders = (await s.execute(select(func.count()).select_from(Order))).scalar_one()
    assert n_orders == 1
    await c.signals.stop()
    await c.aclose()


async def test_stop_loss_and_source_sell_race_close_once(tmp_path, template_db):
    c = await seeded_container(tmp_path, template_db)
    c.signals.start()
    async with c.db.session() as s:
        wallet = next(w for w in await WalletRepo(s).list() if w.selected)
    mint = good_tokens(c, 1)[0]
    buy = live_swap(c, wallet.address, mint, Side.BUY, 700.0, sig="race-buy")
    await c.signals.on_swap(buy)
    await c.signals.drain()
    async with c.db.session() as s:
        pos = (await s.execute(select(Position))).scalar_one()
    market = c.providers.simulated_market
    original = market.token_price
    market.token_price = lambda m, t=None: (original(m, t) or 0) * (0.3 if m == mint else 1)  # type: ignore
    c.tokens._price_cache.clear()
    sell = live_swap(
        c,
        wallet.address,
        mint,
        Side.SELL,
        200.0,
        qty=buy.token_amount,
        before=buy.token_amount,
        after=0.0,
        sig="race-sell",
    )
    await asyncio.gather(c.positions.check_once(), c.signals.on_swap(sell), c.positions.close_position(pos.id))
    await c.signals.drain()
    async with c.db.session() as s:
        exits = (
            await s.execute(
                select(func.count()).select_from(Order).where(Order.purpose == "exit", Order.status == "confirmed")
            )
        ).scalar_one()
        pos = (await s.execute(select(Position))).scalar_one()
    assert exits == 1
    assert pos.status == PositionStatus.CLOSED.value and pos.qty_raw == 0
    await c.signals.stop()
    await c.aclose()
