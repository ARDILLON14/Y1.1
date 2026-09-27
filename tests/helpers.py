"""Test builders shared by unit and integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from copytrader.core.models import ClosedTrade, SwapEvent
from copytrader.core.types import Side, TxSource

T0 = datetime(2025, 3, 1, tzinfo=UTC)
SOL = "So11111111111111111111111111111111111111112"


def swap(
    wallet: str,
    mint: str,
    side: Side,
    t: datetime,
    qty: float,
    usd: float,
    *,
    sig: str | None = None,
    before: float | None = None,
    after: float | None = None,
    liquidity: float | None = None,
) -> SwapEvent:
    return SwapEvent(
        wallet=wallet,
        signature=sig or f"{wallet}-{mint}-{side.value}-{t.timestamp()}",
        slot=int(t.timestamp()),
        block_time=t,
        token_mint=mint,
        side=side,
        token_amount=qty,
        token_decimals=6,
        quote_mint=SOL,
        quote_amount=usd / 100.0,
        price_quote=(usd / 100.0) / qty,
        price_usd=usd / qty,
        value_usd=usd,
        sol_price_usd=100.0,
        token_balance_before=before,
        token_balance_after=after,
        source=TxSource.BACKFILL,
        liquidity_usd=liquidity,
    )


def trade(
    ret: float,
    *,
    cost: float = 100.0,
    start: datetime = T0,
    hold_minutes: float = 60.0,
    mint: str = "M",
    wallet: str = "W",
    regime: str | None = None,
    category: str | None = None,
) -> ClosedTrade:
    pnl = cost * ret
    return ClosedTrade(
        wallet=wallet,
        token_mint=mint,
        opened_at=start,
        closed_at=start + timedelta(minutes=hold_minutes),
        cost_usd=cost,
        proceeds_usd=cost + pnl,
        pnl_usd=pnl,
        return_frac=ret,
        holding_seconds=hold_minutes * 60,
        n_buys=1,
        n_sells=1,
        entry_price_usd=1.0,
        category=category,
        regime=regime,
    )


def trades_series(returns: list[float], *, spacing_hours: float = 12.0, **kw: object) -> list[ClosedTrade]:
    return [
        trade(r, start=T0 + timedelta(hours=i * spacing_hours), mint=f"M{i % 7}", **kw)  # type: ignore[arg-type]
        for i, r in enumerate(returns)
    ]
