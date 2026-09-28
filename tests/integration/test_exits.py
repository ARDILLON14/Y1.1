"""Adaptive exit profile at entry, and the protective exits: liquidity collapse, several wallets selling."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select

from copytrader.core.types import ListType, PositionStatus, Side, SignalStatus, WalletStatus
from copytrader.db.models import Order, Position, Signal
from copytrader.db.repositories import TransactionRepo, WalletRepo
from copytrader.signals.engine import WalletInfo
from tests.integration.conftest import good_token, live_swap, seeded_container


async def _selected_wallet(c):
    async with c.db.session() as s:
        return next(w for w in await WalletRepo(s).list() if w.selected)


async def _open_position(c, sig: str) -> Position:
    c.signals.start()
    wallet = await _selected_wallet(c)
    await c.signals.on_swap(live_swap(c, wallet.address, good_token(c), Side.BUY, 500.0, sig=sig))
    await c.signals.drain()
    async with c.db.session() as s:
        pos = (await s.execute(select(Position))).scalar_one()
    assert pos.status == PositionStatus.OPEN.value
    return pos


async def _position(c, pid: int) -> Position:
    async with c.db.session() as s:
        return await s.get(Position, pid)


async def test_entry_gets_a_volatility_stop_and_is_sized_for_it(tmp_path, template_db, monkeypatch):
    c = await seeded_container(tmp_path, template_db, {"exits": {"volatility_stop": True}})
    try:
        await _check_volatility_stop(c, monkeypatch)
    finally:
        await c.aclose()


async def _check_volatility_stop(c, monkeypatch) -> None:
    original = c.tokens.get

    async def volatile(mint, **kw):  # moving 10 % an hour
        return replace(await original(mint, **kw), price_change_pct={"h1": 10.0})

    monkeypatch.setattr(c.tokens, "get", volatile)
    pos = await _open_position(c, "vol")
    stop = pos.exit_params["adaptive"]["stop_loss_pct"]
    assert 8.0 <= stop <= 35.0 and pos.exit_params["stop_loss_pct"] == stop
    assert pos.exit_params["entry_liquidity_usd"] > 0
    async with c.db.session() as s:
        sig = (await s.execute(select(Signal))).scalar_one()
    check = next(ch for ch in sig.decision["checks"] if ch["name"] == "exit_profile")
    assert check["value"] == pytest.approx(stop, abs=0.05) and "volatilidad 10.0%/h" in check["message"]
    steps = {st["name"]: st for st in sig.decision["sizing"]["steps"]}
    assert f"stop {stop:.0f}%" in steps["base"]["label"]  # risk per trade / adaptive stop
    assert "volatility" not in steps  # the stop already accounts for it: no double reduction
    # the capital at risk is the size times the stop actually used
    assert pos.at_risk_usd == pytest.approx(sig.decision["sizing"]["size_usd"] * stop / 100, rel=0.02)


async def test_default_entry_keeps_the_global_exits(container):
    pos = await _open_position(container, "plain")
    assert pos.exit_params["adaptive"] == {}
    assert pos.exit_params["stop_loss_pct"] == container.cfg.exits.stop_loss_pct


async def test_adaptive_stop_closes_before_the_global_one(container):
    c = container
    pos = await _open_position(c, "tight")
    async with c.db.session() as s:
        row = await s.get(Position, pos.id)
        row.exit_params = {**row.exit_params, "adaptive": {"stop_loss_pct": 10.0}}
    market = c.providers.simulated_market
    original = market.token_price
    market.token_price = lambda m, t=None: (original(m, t) or 0) * (0.86 if m == pos.token_mint else 1)  # type: ignore[method-assign]
    c.tokens._price_cache.clear()
    await c.positions.check_once()  # -14 %: inside the global 20 % stop, beyond this position's 10 %
    closed = await _position(c, pos.id)
    assert closed.status == PositionStatus.CLOSED.value and "Stop loss" in closed.close_reason


async def test_liquidity_collapse_closes_after_confirmation(container, monkeypatch):
    c = container
    pos = await _open_position(c, "liq")
    original = c.tokens.get_many

    async def drained(mints, **kw):
        infos = await original(mints, **kw)
        return {m: replace(i, liquidity_usd=(i.liquidity_usd or 0) * 0.3) for m, i in infos.items()}

    monkeypatch.setattr(c.tokens, "get_many", drained)
    await c.positions.check_once()
    assert (await _position(c, pos.id)).status == PositionStatus.OPEN.value  # 1st observation: could be a glitch
    c.positions._liquidity_checked_at = float("-inf")
    await c.positions.check_once()
    closed = await _position(c, pos.id)
    assert closed.status == PositionStatus.CLOSED.value and "Liquidez -70%" in closed.close_reason
    async with c.db.session() as s:
        order = (await s.execute(select(Order).where(Order.trigger == "liquidity_drop"))).scalar_one()
    assert order.context["fee_decision"]["urgent"]  # a protective exit pays for priority


async def test_several_credible_wallets_selling_trigger_one_partial_exit(tmp_path, template_db):
    c = await seeded_container(
        tmp_path, template_db, {"exits": {"wallet_sells_exit_fraction": 0.5, "wallet_sells_exit_min": 2}}
    )
    try:
        pos = await _open_position(c, "ws")
        async with c.db.session() as s:
            others = [w for w in await WalletRepo(s).list() if w.id != pos.source_wallet_id][:3]
        blocked_id = others[2].id

        def info(wallet_id: int) -> WalletInfo:
            status = WalletStatus.BLOCKED if wallet_id == blocked_id else WalletStatus.ACTIVE
            return WalletInfo(wallet_id, f"addr{wallet_id}", f"w{wallet_id}", ListType.NONE, status, 60.0, False)

        c.positions.wallet_info = info

        async def sell(wallet, sig: str, before: float, after: float) -> None:
            ev = live_swap(c, wallet.address, pos.token_mint, Side.SELL, 200.0, before=before, after=after, sig=sig)
            async with c.db.session() as s:
                await TransactionRepo(s).insert_swap(wallet.id, replace(ev, block_time=c.clock.now()))

        await sell(others[0], "s1", 100.0, 20.0)  # one credible wallet out
        await sell(others[2], "s2", 100.0, 0.0)  # a blocked wallet does not count
        await sell(others[1], "s3a", 100.0, 70.0)  # 30 %...
        await c.positions.check_once()
        assert (await _position(c, pos.id)).qty_raw == pos.qty_raw
        await sell(others[1], "s3b", 70.0, 40.0)  # ...and 30 % more = 60 % of its holding: 2 credible wallets
        await c.positions.check_once()
        after = await _position(c, pos.id)
        assert after.status == PositionStatus.OPEN.value and after.qty_raw == pytest.approx(pos.qty_raw / 2, rel=0.01)
        assert "wallets_selling" in after.exit_params["done"]
        await c.positions.check_once()  # a partial exit happens once
        assert (await _position(c, pos.id)).qty_raw == after.qty_raw
        async with c.db.session() as s:
            sig = (await s.execute(select(Signal))).scalars().first()
        assert sig.status == SignalStatus.EXECUTED.value
    finally:
        await c.aclose()
