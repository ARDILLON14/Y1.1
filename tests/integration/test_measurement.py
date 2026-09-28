"""Measurement: outcomes of executed and rejected signals, attribution of results."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from copytrader.core.types import Side, SignalStatus
from copytrader.db.models import Signal, SignalOutcome
from copytrader.db.repositories import WalletRepo
from tests.integration.conftest import good_token, live_swap


async def _selected_wallet(c):
    async with c.db.session() as s:
        return next(w for w in await WalletRepo(s).list() if w.selected)


async def _outcomes(c):
    async with c.db.session() as s:
        rows = (await s.execute(select(SignalOutcome, Signal).join(Signal))).all()
    return {sig.status: out for out, sig in rows}


async def test_outcomes_follow_executed_and_rejected_signals(container):
    c = container
    c.signals.start()
    wallet = await _selected_wallet(c)
    mint = good_token(c)
    await c.signals.on_swap(live_swap(c, wallet.address, mint, Side.BUY, 500.0, sig="ok"))
    await c.signals.on_swap(live_swap(c, wallet.address, mint, Side.BUY, 500.0, sig="old", age_seconds=300))
    await c.signals.drain()

    t0 = c.clock.now()
    enrolled, _ = await c.outcomes.run_once(t0 + timedelta(minutes=6))
    assert enrolled == 2
    assert (await c.outcomes.run_once(t0 + timedelta(minutes=7)))[0] == 0  # idempotent enrolment
    rows = await _outcomes(c)
    executed, expired = rows[SignalStatus.EXECUTED.value], rows[SignalStatus.EXPIRED.value]
    assert executed.failed_check is None
    assert expired.failed_check == "signal_age" and expired.failed_label == "Retraso de la señal aceptable"
    for row in (executed, expired):
        assert row.reference_price_usd > 0
        assert isinstance(row.returns["5"], float) and not row.completed

    await c.outcomes.run_once(t0 + timedelta(minutes=61))
    await c.outcomes.run_once(t0 + timedelta(hours=24, minutes=30))
    rows = await _outcomes(c)
    for row in rows.values():
        assert set(row.returns) == {"5", "60", "1440"} and row.completed
        assert all(isinstance(v, float) for v in row.returns.values())


async def test_a_horizon_sampled_far_too_late_is_recorded_as_missed(container):
    c = container
    c.signals.start()
    wallet = await _selected_wallet(c)
    await c.signals.on_swap(live_swap(c, wallet.address, good_token(c), Side.BUY, 500.0, sig="late"))
    await c.signals.drain()
    t0 = c.clock.now()
    assert (await c.outcomes.run_once(t0 + timedelta(minutes=1)))[0] == 1  # enrolled, nothing due yet
    await c.outcomes.run_once(t0 + timedelta(days=3))  # the app was down: nothing can be sampled honestly
    (row,) = (await _outcomes(c)).values()
    assert row.returns == {"5": None, "60": None, "1440": None} and row.completed


async def test_attribution_report_explains_where_the_result_comes_from(container):
    from copytrader.measurement.attribution import AttributionService

    c = container
    c.signals.start()
    wallet = await _selected_wallet(c)
    mint = good_token(c)
    buy = live_swap(c, wallet.address, mint, Side.BUY, 800.0, before=0.0, sig="a-buy")
    await c.signals.on_swap(buy)
    await c.signals.drain()
    qty = buy.token_amount
    sell = live_swap(c, wallet.address, mint, Side.SELL, 820.0, qty=qty, before=qty, after=0.0, sig="a-sell")
    await c.signals.on_swap(sell)
    await c.signals.on_swap(live_swap(c, wallet.address, mint, Side.BUY, 500.0, sig="a-old", age_seconds=300))
    await c.signals.drain()
    t0 = c.clock.now()
    await c.outcomes.run_once(t0 + timedelta(minutes=6))

    report = await AttributionService(c.db).report(
        mode="paper", since=t0 - timedelta(days=1), horizons=c.cfg.measurement.outcome_horizons_minutes
    )
    assert report["horizons"] == ["5", "60", "1440"]
    filters = {r["check"]: r for r in report["filters"]}
    assert filters["executed"]["n"] == 1 and filters["executed"]["horizons"]["5"]["n"] == 1
    assert filters["signal_age"]["verdict"] == "Muestra insuficiente"
    (w,) = report["wallets"]
    assert w["wallet"] == wallet.address and w["n"] == 1 and w["estimated_copy_pct"] is not None
    (exit_row,) = report["exits"]
    assert exit_row["trigger"] == "source_sell" and exit_row["n"] == 1
    assert sum(b["entries"] for b in report["delay"]) == 1
    assert sum(b["closed"] for b in report["delay"]) == 1
    costs = report["costs"]
    assert costs["positions"] == 1 and costs["fees_usd"] > 0
    assert costs["gross_pnl_usd"] == pytest.approx(costs["net_pnl_usd"] + costs["fees_usd"])
    assert costs["entries_measured"] == 1 and costs["entry_late_pct"] is not None


async def test_evaluation_learns_from_real_copies(container):
    from copytrader.db.models import Position
    from copytrader.db.repositories import AnalyticsRepo

    c = container
    wallet = await _selected_wallet(c)
    now = c.clock.now()
    async with c.db.session() as s:
        for i in range(12):  # copying this wallet has consistently lost ~5 % per position
            s.add(
                Position(
                    mode="paper",
                    token_mint=f"LOSS{i}",
                    decimals=6,
                    source_wallet_id=wallet.id,
                    exit_mode="protected",
                    status="closed",
                    qty_raw=0,
                    initial_qty_raw=10**6,
                    cost_usd=0,
                    initial_cost_usd=40,
                    entry_price_usd=1,
                    peak_price_usd=1,
                    realized_pnl_usd=-2.0 - 0.1 * (i % 3),
                    opened_at=now - timedelta(hours=i + 2),
                    closed_at=now - timedelta(hours=i + 1),
                )
            )
    await c.cycle.run()
    async with c.db.session() as s:
        w = await WalletRepo(s).get(wallet.id)
        m = (await AnalyticsRepo(s).latest_metrics(wallet.id, "all")).data
    assert m["realized_copy_n"] == 12 and m["realized_copy_mean_pct"] < 0
    assert m["effective_copy_expectancy_pct"] < m["copy_expectancy_pct"]
    assert w.status == "observe" and not w.selected
    assert any("pierde en la práctica" in r for r in w.status_reasons)


async def test_live_money_only_after_a_paper_probation(container, monkeypatch):
    from copytrader.core.types import TradeMode
    from copytrader.db.models import Position
    from copytrader.execution.mode import ModeController

    c = container
    monkeypatch.setattr(ModeController, "trade_mode", property(lambda self: TradeMode.LIVE))  # "armed" live level
    c.signals.start()
    wallet = await _selected_wallet(c)
    mint = good_token(c)

    await c.signals.on_swap(live_swap(c, wallet.address, mint, Side.BUY, 500.0, sig="p1"))
    await c.signals.drain()
    async with c.db.session() as s:
        sig = (await s.execute(select(Signal).where(Signal.source_signature == "p1"))).scalar_one()
    probation = next(ch for ch in sig.decision["checks"] if ch["name"] == "probation")
    assert sig.status == SignalStatus.EXECUTED.value and sig.mode == "paper"  # copied, but in PAPER
    assert not probation["passed"] and "en prueba: 0/5" in probation["message"]

    now = c.clock.now()
    async with c.db.session() as s:
        for i in range(5):  # five closed paper copies with a positive average
            s.add(
                Position(
                    mode="paper",
                    token_mint=f"WIN{i}",
                    decimals=6,
                    source_wallet_id=wallet.id,
                    exit_mode="protected",
                    status="closed",
                    qty_raw=0,
                    initial_qty_raw=10**6,
                    cost_usd=0,
                    initial_cost_usd=40,
                    entry_price_usd=1,
                    peak_price_usd=1,
                    realized_pnl_usd=3.0,
                    opened_at=now - timedelta(hours=i + 2),
                    closed_at=now - timedelta(hours=i + 1),
                )
            )
    await c.signals.on_swap(live_swap(c, wallet.address, good_token(c), Side.BUY, 500.0, sig="p2"))
    await c.signals.drain()
    async with c.db.session() as s:
        sig = (await s.execute(select(Signal).where(Signal.source_signature == "p2"))).scalar_one()
    probation = next(ch for ch in sig.decision["checks"] if ch["name"] == "probation")
    assert probation["passed"] and "superado: 5 copias" in probation["message"]
    assert sig.mode == "live"  # graduated: this entry goes to the live book
