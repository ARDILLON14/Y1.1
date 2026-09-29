"""Backtesting with real candles: download once, cache, and stops that happen between trades."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from copytrader.backtest.engine import Backtester, BacktestParams, _Book
from copytrader.backtest.history import PriceHistory, PriceNeed, load_price_history
from copytrader.config.loader import build_config
from copytrader.core.errors import ProviderError
from copytrader.core.types import ExitMode, Side
from copytrader.providers.geckoterminal import Candle
from tests.helpers import swap

T0 = datetime(2026, 8, 1, tzinfo=UTC)
NOW = T0 + timedelta(days=10)
STEP = timedelta(minutes=15)


def _flat(start: datetime, n: int, price: float = 1.0) -> list[Candle]:
    return [Candle(start + STEP * i, price, price, price, price, 10.0) for i in range(n)]


class FakeGecko:
    source = "geckoterminal"

    def __init__(self, data: dict[str, list[Candle]], missing: set[str], failing: set[str]) -> None:
        self.data = data
        self.missing = missing
        self.failing = failing
        self.calls: list[tuple[str, str]] = []

    async def top_pool(self, mint: str) -> str | None:
        self.calls.append(("pool", mint))
        if mint in self.failing:
            raise ProviderError("rate limited", provider="gt", status_code=429)
        if mint in self.missing:
            raise ProviderError("not found", provider="gt", status_code=404)
        return f"pool-{mint}"

    async def candles(self, pool: str, mint: str, start: datetime, end: datetime, minutes: int) -> list[Candle]:
        self.calls.append(("candles", mint))
        return [c for c in self.data.get(mint, []) if start <= c.ts <= end]


async def test_candles_are_downloaded_once_cached_and_misses_not_repeated(container):
    c = container
    end = T0 + timedelta(days=2)
    needs = [PriceNeed(m, T0, end, 3 - i) for i, m in enumerate(("A", "B", "C"))]
    gecko = FakeGecko({"A": _flat(T0, 4 * 24 * 3)}, missing={"B"}, failing={"C"})

    async def load(now: datetime) -> PriceHistory:
        return await load_price_history(c.db, gecko, needs, 15, now=now, refetch_failed_after=timedelta(hours=24))  # type: ignore[arg-type]

    h = await load(NOW)
    assert h.has_candles("A") and not h.has_candles("B") and not h.has_candles("C")
    assert h.stats["tokens_with_prices"] == 1 and h.stats["fetched_now"] == 2 and h.stats["failed"] == 1
    gecko.calls.clear()
    h2 = await load(NOW + timedelta(hours=1))
    assert h2.has_candles("A")  # from the database cache
    assert gecko.calls == [("pool", "C")]  # only the transient failure is retried; B had no history
    gecko.calls.clear()
    await load(NOW + timedelta(hours=30))  # after the retry window the missing token is asked again
    assert ("pool", "B") in gecko.calls and ("pool", "A") not in gecko.calls

    # a later backtest that needs a longer range downloads only the missing tail, with the known pool
    gecko.data["A"] = _flat(T0, 4 * 24 * 5)
    gecko.calls.clear()
    longer = [PriceNeed("A", T0, T0 + timedelta(days=4), 3)]
    h3 = await load_price_history(c.db, gecko, longer, 15, now=NOW, refetch_failed_after=timedelta(hours=24))  # type: ignore[arg-type]
    assert gecko.calls == [("candles", "A")]
    assert h3.price_at("A", T0 + timedelta(days=3, hours=12)) == 1.0


def test_stop_between_two_trades_is_found_in_the_candles():
    cfg = build_config({})
    history = PriceHistory(15)
    candles = _flat(T0, 12)
    candles[4] = Candle(T0 + STEP * 4, 1.0, 1.02, 0.7, 0.97, 10.0)  # a dip at 01:00 that recovers
    history.add_candles("MINT", candles)
    bt = Backtester(lambda: cfg, history=history)
    book = _Book(1000.0, 1000.0)
    buy = swap("w", "MINT", Side.BUY, T0 + timedelta(minutes=1), 100, 100)  # at 1.0 USD
    params = BacktestParams(entry_slippage_pct=0, exit_slippage_pct=0, latency_seconds=0, exit_mode=ExitMode.PROTECTED)
    bt._simulate(book, [(buy, True)], params, {"w": 80.0}, T0, T0 + timedelta(hours=3))
    assert len(book.trades) == 1
    trade = book.trades[0]
    assert trade["reason"] == "stop_loss"
    # filled at the stop level (-20 %), not at the candle's low (-30 %) nor its close (-3 %)
    assert -21.5 < trade["return_pct"] < -19.5
    assert trade["closed_at"] == (T0 + STEP * 5).isoformat()
    # the same trade without candles never stops out: the price is only known at the trades
    blind = Backtester(lambda: cfg)
    book2 = _Book(1000.0, 1000.0)
    blind._simulate(book2, [(buy, True)], params, {"w": 80.0}, T0, T0 + timedelta(hours=3))
    assert book2.trades == [] and "MINT" in book2.positions
