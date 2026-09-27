from __future__ import annotations

from datetime import timedelta

from copytrader.core.models import TokenInfo
from copytrader.core.types import ExitMode, KillSwitchScope, TradeMode
from copytrader.db.models import EquitySnapshot, Order, Position
from copytrader.risk.engine import EntryRequest
from tests.integration.conftest import seeded_container


def _pos(c, pnl: float, *, minutes_ago: float = 5, status: str = "closed") -> Position:
    now = c.clock.now()
    return Position(
        mode="paper",
        token_mint=f"M{pnl}{minutes_ago}",
        decimals=6,
        exit_mode="protected",
        status=status,
        qty_raw=0 if status == "closed" else 10**6,
        initial_qty_raw=10**6,
        cost_usd=0 if status == "closed" else 50,
        initial_cost_usd=50,
        entry_price_usd=1,
        peak_price_usd=1,
        realized_pnl_usd=pnl,
        opened_at=now - timedelta(minutes=minutes_ago + 10),
        closed_at=now - timedelta(minutes=minutes_ago) if status == "closed" else None,
        at_risk_usd=10 if status != "closed" else 0,
    )


def _token(c) -> TokenInfo:
    return TokenInfo(
        mint="TOKEN" * 8,
        fetched_at=c.clock.now(),
        price_usd=1.0,
        liquidity_usd=1e6,
        market_cap_usd=1e7,
        category="cap:mid",
    )


async def test_daily_loss_trips_daily_kill_switch(tmp_path, template_db):
    c = await seeded_container(tmp_path, template_db)
    day_start = c.clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
    async with c.db.session() as s:
        s.add(
            EquitySnapshot(
                ts=day_start - timedelta(minutes=1),
                mode="paper",
                equity_usd=1000,
                cash_usd=1000,
                exposure_usd=0,
                realized_pnl_usd=0,
                unrealized_pnl_usd=0,
            )
        )
        s.add(_pos(c, -60.0))
    book = await c.risk.enforce_limits(TradeMode.PAPER)
    assert book.loss_pct(book.day_start_equity) >= 5
    assert c.kill.is_active(KillSwitchScope.DAILY)
    d = await c.risk.evaluate_entry(EntryRequest(TradeMode.PAPER, _token(c), 1, 80, ExitMode.PROTECTED, False))
    assert not d.approved and any(ch.name == "kill_switch" and not ch.passed for ch in d.checks)
    await c.aclose()


async def test_consecutive_losses_trip_global_and_reset_starts_fresh(tmp_path, template_db):
    c = await seeded_container(
        tmp_path,
        template_db,
        {
            "risk": {
                "max_consecutive_losses": 3,
                "max_daily_loss_pct": 40,
                "max_weekly_loss_pct": 40,
                "max_monthly_loss_pct": 40,
            }
        },
    )
    async with c.db.session() as s:
        for i in range(3):
            s.add(_pos(c, -1.0, minutes_ago=30 - i))
    await c.risk.enforce_limits(TradeMode.PAPER)
    assert c.kill.is_active(KillSwitchScope.GLOBAL)
    await c.kill.deactivate(KillSwitchScope.GLOBAL, actor="test")
    await c.risk.enforce_limits(TradeMode.PAPER)
    assert not c.kill.is_active(KillSwitchScope.GLOBAL)  # old streak does not re-trip after manual reset
    await c.aclose()


async def test_reservations_and_inflight_orders_consume_capacity(tmp_path, template_db):
    c = await seeded_container(tmp_path, template_db, {"risk": {"max_open_positions": 2}})
    req = EntryRequest(TradeMode.PAPER, _token(c), 1, 80, ExitMode.PROTECTED, False)
    d1 = await c.risk.evaluate_entry(req)
    assert d1.approved and d1.reservation_id
    async with c.db.session() as s:  # an unfilled order from before a restart
        s.add(
            Order(
                client_order_id="inflight",
                mode="paper",
                purpose="entry",
                side="buy",
                token_mint="X" * 40,
                input_mint="S",
                output_mint="X",
                amount_in_raw=1,
                slippage_bps=100,
                status="submitted",
                notional_usd=30.0,
                context={"at_risk_usd": 6},
            )
        )
    d2 = await c.risk.evaluate_entry(req)
    assert not d2.approved and any(ch.name == "open_positions" and not ch.passed for ch in d2.checks)
    c.risk.release(d1.reservation_id)
    d3 = await c.risk.evaluate_entry(req)
    assert d3.approved
    await c.aclose()


async def test_per_wallet_risk_limit(tmp_path, template_db):
    c = await seeded_container(tmp_path, template_db, {"risk": {"max_risk_per_wallet_pct": 1.0}})
    async with c.db.session() as s:
        p = _pos(c, 0.0, status="open")
        p.source_wallet_id = 1
        p.at_risk_usd = 9.5
        s.add(p)
    d = await c.risk.evaluate_entry(EntryRequest(TradeMode.PAPER, _token(c), 1, 90, ExitMode.PROTECTED, False))
    # only 0.5 USD of risk budget left for wallet 1 → size 2.5 USD < minimum → rejected
    assert not d.approved and "mínimo" in (d.reason or "")
    other = await c.risk.evaluate_entry(EntryRequest(TradeMode.PAPER, _token(c), 2, 90, ExitMode.PROTECTED, False))
    assert other.approved
    await c.aclose()
