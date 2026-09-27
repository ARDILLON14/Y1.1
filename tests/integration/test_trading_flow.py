from __future__ import annotations

from sqlalchemy import func, select

from copytrader.core.types import KillSwitchScope, PositionStatus, Side, SignalStatus, TradeMode, WalletStatus
from copytrader.db.models import Execution, Order, Position, Signal, WalletTransaction
from copytrader.db.repositories import WalletRepo
from tests.integration.conftest import good_token, live_swap, seeded_container


async def _selected_wallet(c):
    async with c.db.session() as s:
        wallets = await WalletRepo(s).list()
    selected = [w for w in wallets if w.selected]
    assert selected, "the evaluation cycle should select at least one wallet"
    return selected[0]


async def _signals(c):
    async with c.db.session() as s:
        return list((await s.execute(select(Signal).order_by(Signal.id))).scalars().all())


async def _positions(c):
    async with c.db.session() as s:
        return list((await s.execute(select(Position).order_by(Position.id))).scalars().all())


async def test_archetypes_are_classified(container):
    c = container
    market = c.providers.simulated_market
    async with c.db.session() as s:
        wallets = {w.address: w for w in await WalletRepo(s).list()}
    by_arch = {}
    for addr, w in market.wallets.items():
        by_arch.setdefault(w.archetype, []).append(wallets[addr])
    for w in by_arch["wash"] + by_arch["sniper"] + by_arch["coordinated"]:
        assert w.status == WalletStatus.BLOCKED.value, (w.label, w.status_reasons)
        assert not w.selected
    assert any(w.selected for w in by_arch["skilled"])
    assert all(w.status != WalletStatus.ACTIVE.value for w in by_arch["inactive"])
    # Randomness can make a no-edge wallet look decent, but never better than genuinely skilled ones.
    edge = [w.score for w in by_arch["skilled"] + by_arch["scalper"]]
    noise = [w.score for w in by_arch["random"]]
    assert sum(edge) / len(edge) > sum(noise) / len(noise) + 5
    top = min((w for w in wallets.values() if w.rank), key=lambda w: w.rank)
    assert market.wallets[top.address].archetype in ("skilled", "scalper")


async def test_buy_is_copied_in_paper_and_mirror_sell_closes(container):
    c = container
    c.signals.start()
    wallet = await _selected_wallet(c)
    mint = good_token(c)

    buy = live_swap(c, wallet.address, mint, Side.BUY, 800.0, before=0.0, after=None)
    await c.signals.on_swap(buy)
    await c.signals.drain()
    sigs = await _signals(c)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.status == SignalStatus.EXECUTED.value, sig.decision
    names = [chk["name"] for chk in sig.decision["checks"]]
    for expected in (
        "wallet_eligible",
        "min_score",
        "signal_age",
        "liquidity",
        "token_risk",
        "slippage",
        "total_exposure",
        "signal_ttl",
        "execution",
    ):
        assert expected in names
    positions = await _positions(c)
    assert len(positions) == 1 and positions[0].status == PositionStatus.OPEN.value
    assert positions[0].mode == TradeMode.PAPER.value
    assert positions[0].cost_usd <= c.cfg.risk.max_trade_usd * 1.01  # never the source's $800

    # Duplicate notification: ignored completely
    await c.signals.on_swap(buy)
    await c.signals.drain()
    assert len(await _signals(c)) == 1

    # Source sells everything → mirrored exit (PROTECTED mode follows source exits)
    qty = buy.token_amount
    sell = live_swap(c, wallet.address, mint, Side.SELL, 820.0, qty=qty, before=qty, after=0.0, sig="sell-1")
    await c.signals.on_swap(sell)
    await c.signals.drain()
    positions = await _positions(c)
    assert positions[0].status == PositionStatus.CLOSED.value
    assert "origen" in (positions[0].close_reason or "")
    async with c.db.session() as s:
        n_exec = (await s.execute(select(func.count()).select_from(Execution))).scalar_one()
        n_orders = (await s.execute(select(func.count()).select_from(Order))).scalar_one()
        n_tx = (await s.execute(select(func.count()).select_from(WalletTransaction))).scalar_one()
    assert n_exec == 2 and n_orders == 2
    assert n_tx > 0


async def test_rejections_are_explained(container):
    c = container
    c.signals.start()
    wallet = await _selected_wallet(c)
    mint = good_token(c)

    # Too old: the trade already happened a minute ago
    old = live_swap(c, wallet.address, mint, Side.BUY, 500.0, age_seconds=120, sig="old")
    await c.signals.on_swap(old)
    await c.signals.drain()
    sig = (await _signals(c))[-1]
    assert sig.status == SignalStatus.EXPIRED.value
    assert "Retraso" in sig.reason

    # Kill switch blocks new entries
    await c.kill.activate(KillSwitchScope.GLOBAL, "test", actor="pytest")
    await c.signals.on_swap(live_swap(c, wallet.address, mint, Side.BUY, 500.0, sig="ks"))
    await c.signals.drain()
    sig = (await _signals(c))[-1]
    assert sig.status == SignalStatus.REJECTED.value
    assert "Kill switch" in sig.reason
    assert not await _positions(c)


async def test_blacklisted_wallet_never_copies(container):
    c = container
    c.signals.start()
    wallet = await _selected_wallet(c)
    from copytrader.core.types import ListType

    await c.collector.set_list(wallet.address, ListType.BLACKLIST)
    await c.refresh_tracking()
    await c.signals.on_swap(live_swap(c, wallet.address, good_token(c), Side.BUY, 500.0, sig="bl"))
    await c.signals.drain()
    assert await _signals(c) == []  # classified IGNORE: not even a signal
    assert await _positions(c) == []


async def test_stop_loss_closes_position(container):
    c = container
    c.signals.start()
    wallet = await _selected_wallet(c)
    mint = good_token(c)
    await c.signals.on_swap(live_swap(c, wallet.address, mint, Side.BUY, 500.0, sig="sl-buy"))
    await c.signals.drain()
    assert (await _positions(c))[0].status == PositionStatus.OPEN.value

    market = c.providers.simulated_market
    original = market.token_price

    def crashed(m, t=None):
        p = original(m, t)
        return p * 0.5 if (m == mint and p) else p

    market.token_price = crashed  # type: ignore[method-assign]
    c.tokens._price_cache.clear()
    await c.positions.check_once()
    pos = (await _positions(c))[0]
    assert pos.status == PositionStatus.CLOSED.value
    assert "emergencia" in pos.close_reason.lower() or "stop" in pos.close_reason.lower()
    assert pos.realized_pnl_usd < 0


async def test_restart_recovery_expires_pending_entries(tmp_path, template_db):
    from tests.integration.conftest import make_container

    c = await seeded_container(tmp_path, template_db)
    wallet = await _selected_wallet(c)
    # Signal persisted but never processed (engine workers not started) → simulates a crash.
    await c.signals.on_swap(live_swap(c, wallet.address, good_token(c), Side.BUY, 500.0, sig="crash"))
    await c.aclose()

    c2 = await make_container(tmp_path)
    await c2.refresh_tracking()
    counts = await c2.signals.recover()
    assert counts["expired_entries"] == 1
    sig = (await _signals(c2))[-1]
    assert sig.status == SignalStatus.EXPIRED.value
    await c2.aclose()
