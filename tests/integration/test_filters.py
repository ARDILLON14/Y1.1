"""Per-signal filters: RugCheck risks, sell route, confluence, market regime, expected value."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from sqlalchemy import select

from copytrader.core.errors import ProviderError
from copytrader.core.types import Side, SignalStatus, TradeMode
from copytrader.db.models import Signal
from copytrader.db.repositories import WalletRepo
from tests.integration.conftest import good_token, live_swap


async def _selected_wallet(c):
    async with c.db.session() as s:
        return next(w for w in await WalletRepo(s).list() if w.selected)


async def _copy(c, sig: str, **kw):
    wallet = kw.pop("wallet", None) or await _selected_wallet(c)
    mint = kw.pop("mint", None) or good_token(c)
    await c.signals.on_swap(live_swap(c, wallet.address, mint, Side.BUY, kw.pop("usd", 500.0), sig=sig, **kw))
    await c.signals.drain()
    async with c.db.session() as s:
        return (await s.execute(select(Signal).where(Signal.source_signature == sig))).scalar_one()


def _check(sig: Signal, name: str) -> dict:
    return next(ch for ch in sig.decision["checks"] if ch["name"] == name)


async def test_rugcheck_risks_block_the_token(container, monkeypatch):
    c = container
    c.signals.start()
    original = c.tokens.get

    async def with_flags(mint, **kw):
        info = await original(mint, **kw)
        return replace(info, risk_flags=["warn:Top 10 Holders High Ownership", "warn:Mutable metadata"])

    monkeypatch.setattr(c.tokens, "get", with_flags)
    sig = await _copy(c, "rc")
    assert sig.status == SignalStatus.REJECTED.value
    check = _check(sig, "risk_flags")
    assert not check["passed"] and check["message"] == "Top 10 Holders High Ownership"  # Mutable metadata allowed


async def test_tokens_without_a_sell_route_are_not_bought(container, monkeypatch):
    c = container
    c.signals.start()
    mint = good_token(c)
    paper = c.execution.executors[next(iter(c.execution.executors))]
    original = paper.quote

    async def no_exit(input_mint, output_mint, amount_raw, slippage_bps, **kw):
        if input_mint == mint:
            raise ProviderError("jupiter: no route found", provider="jupiter", retryable=False)
        return await original(input_mint, output_mint, amount_raw, slippage_bps, **kw)

    monkeypatch.setattr(paper, "quote", no_exit)
    sig = await _copy(c, "hp", mint=mint)
    assert sig.status == SignalStatus.REJECTED.value
    assert "sin ruta de venta" in _check(sig, "sell_route")["message"]


async def test_sell_route_passes_for_a_normal_token(container):
    c = container
    c.signals.start()
    sig = await _copy(c, "ok")
    check = _check(sig, "sell_route")
    assert sig.status == SignalStatus.EXECUTED.value and check["passed"]
    assert 0 <= check["value"] < 10


async def test_sell_route_is_quoted_with_the_buy_and_reused_for_the_same_token(container, monkeypatch):
    c = container
    c.signals.start()
    mint = good_token(c)
    paper = c.execution.executors[next(iter(c.execution.executors))]
    original = paper.quote
    calls: list[tuple[str, float, float]] = []

    async def slow_quote(input_mint, output_mint, amount_raw, slippage_bps, **kw):
        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.sleep(0.1)
        q = await original(input_mint, output_mint, amount_raw, slippage_bps, **kw)
        calls.append(("sell" if input_mint == mint else "buy", started, loop.time()))
        return q

    monkeypatch.setattr(paper, "quote", slow_quote)
    sig = await _copy(c, "par", mint=mint)
    assert sig.status == SignalStatus.EXECUTED.value and _check(sig, "sell_route")["passed"]
    buy = next(call for call in calls if call[0] == "buy")
    sell = next(call for call in calls if call[0] == "sell")
    assert sell[1] < buy[2]  # the exit was quoted while the buy quote was still running
    # a recent pass for this token (similar size) is reused: no new sell quote
    calls.clear()
    checks: list = []
    quote = await original(c.cfg.execution.quote_mint, mint, 50_000_000, 150)
    ctx = SimpleNamespace(swap=SimpleNamespace(token_mint=mint))
    await c.pipeline._check_sell_route(ctx, quote, checks, TradeMode.PAPER)
    assert not calls and checks[-1].passed and "verificado hace" in checks[-1].message
    # ...but not once the cache time is over
    c.pipeline._sell_ok[mint] = (*c.pipeline._sell_ok[mint][:2], 0.0)
    await c.pipeline._check_sell_route(ctx, quote, checks, TradeMode.PAPER)
    assert [call[0] for call in calls] == ["sell"]


async def _other_wallet(c, exclude_id: int, status: str):
    async with c.db.session() as s:
        return next(w for w in await WalletRepo(s).list() if w.id != exclude_id and w.status == status)


async def _insert_buy(c, wallet, mint: str, sig: str, age_seconds: float) -> None:
    from copytrader.db.repositories import TransactionRepo

    async with c.db.session() as s:
        await TransactionRepo(s).insert_swap(
            wallet.id, live_swap(c, wallet.address, mint, Side.BUY, 300.0, sig=sig, age_seconds=age_seconds)
        )


def _step(sig: Signal, name: str) -> dict | None:
    return next((st for st in sig.decision["sizing"]["steps"] if st["name"] == name), None)


async def test_independent_wallets_buying_the_same_token_raise_the_size(container):
    c = container
    c.signals.start()
    wallet = await _selected_wallet(c)
    mint = good_token(c)
    await _insert_buy(c, await _other_wallet(c, wallet.id, "active"), mint, "other-1", age_seconds=300)
    await _insert_buy(c, await _other_wallet(c, wallet.id, "blocked"), mint, "wash-1", age_seconds=200)
    noise = next(w for w in c.signals._wallets.values() if w.status.value == "observe" and (w.copy_edge_pct or 0) <= 0)
    await _insert_buy(c, noise, mint, "noise-1", age_seconds=100)
    sig = await _copy(c, "conf", wallet=wallet, mint=mint)
    check = _check(sig, "confluence")
    # only the active wallet counts: the blocked one is not independent, the no-edge one is noise
    assert check["value"] == 1 and "1 wallet(s) independientes" in check["message"]
    assert _step(sig, "confluence")["factor"] == 1.25


async def test_confluence_can_be_required(tmp_path, template_db):
    from tests.integration.conftest import seeded_container

    c = await seeded_container(tmp_path, template_db, {"filters": {"min_confluence_wallets": 1}})
    try:
        c.signals.start()
        sig = await _copy(c, "alone")
        assert sig.status == SignalStatus.REJECTED.value
        assert "Confluencia de wallets" in sig.reason
    finally:
        await c.signals.stop()
        await c.aclose()


async def test_market_regime_reduces_size_or_blocks(tmp_path, template_db):
    from tests.integration.conftest import seeded_container

    c = await seeded_container(tmp_path, template_db, {"filters": {"block_regimes": ["extreme_up"]}})
    try:
        c.signals.start()
        c.pipeline.regime = lambda: "extreme_up"
        sig = await _copy(c, "up")
        assert sig.status == SignalStatus.REJECTED.value and "Régimen de mercado" in sig.reason
        c.pipeline.regime = lambda: "extreme_down"
        sig = await _copy(c, "down")
        assert sig.status == SignalStatus.EXECUTED.value
        assert _step(sig, "regime")["factor"] == 0.5 and "caída extrema" in _check(sig, "regime")["message"]
    finally:
        await c.signals.stop()
        await c.aclose()


async def test_expected_value_after_this_copys_own_costs(container):
    c = container
    c.signals.start()
    wallet = await _selected_wallet(c)
    info = c.signals.wallet(wallet.address)
    assert info is not None and info.copy_edge_pct is not None and info.model_cost_pct is not None

    # A thin edge that a small copy's fixed fees wipe out: rejected.
    c.signals._wallets[wallet.address] = replace(info, copy_edge_pct=0.3, model_cost_pct=0.1)
    sig = await _copy(c, "thin")
    check = _check(sig, "expected_value")
    assert sig.status == SignalStatus.REJECTED.value and not check["passed"]
    assert "coste extra de este tamaño" in check["message"] and check["value"] < 0

    # A solid edge survives the same costs.
    c.signals._wallets[wallet.address] = replace(info, copy_edge_pct=8.0, model_cost_pct=0.1)
    sig = await _copy(c, "solid")
    assert sig.status == SignalStatus.EXECUTED.value and _check(sig, "expected_value")["passed"]
