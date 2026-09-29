"""Historical candles: GeckoTerminal client, no look-ahead prices and conservative exits inside a candle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from copytrader.backtest.engine import _Pos, candle_exit
from copytrader.backtest.history import PriceHistory, price_needs
from copytrader.config.loader import build_config
from copytrader.core.types import ExitMode, Side
from copytrader.providers.geckoterminal import Candle, GeckoTerminalClient, parse_ohlcv, timeframe_for
from copytrader.resilience.circuit_breaker import CircuitBreaker
from copytrader.resilience.http import ResilientHttp
from copytrader.resilience.retry import RetryPolicy
from tests.helpers import swap

BASE = "https://gt.test/api/v2"
T = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
MINT = "MintAAA"


def _http() -> ResilientHttp:
    return ResilientHttp(
        "gt", timeout=1, rate_per_second=1000, retry=RetryPolicy(1, 0.001, 0.001), breaker=CircuitBreaker("g", 5, 1)
    )


def _row(ts: datetime, o: float, h: float, lo: float, c: float, v: float = 100.0) -> list[float]:
    return [ts.timestamp(), o, h, lo, c, v]


def test_parse_ohlcv_sorts_and_skips_bad_rows():
    data = {
        "data": {
            "attributes": {
                "ohlcv_list": [
                    _row(T + timedelta(minutes=15), 2, 3, 1.5, 2.5),
                    _row(T, 1, 2.2, 0.9, 2),
                    ["bad"],
                    [T.timestamp(), 1, None, 1, 1, 1],
                    [T.timestamp(), 0, 1, 1, 1, 1],  # zero price: not a real candle
                ]
            }
        }
    }
    candles = parse_ohlcv(data)
    assert [c.ts for c in candles] == [T, T + timedelta(minutes=15)]
    assert candles[0] == Candle(T, 1, 2.2, 0.9, 2, 100.0)
    assert parse_ohlcv({}) == [] and parse_ohlcv(None) == []


def test_timeframes():
    assert timeframe_for(15) == ("minute", 15)
    assert timeframe_for(60) == ("hour", 1)
    with pytest.raises(ValueError):
        timeframe_for(7)


@respx.mock
async def test_client_picks_the_deepest_pool_and_pages_back_in_time():
    respx.get(f"{BASE}/networks/solana/tokens/{MINT}/pools").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"attributes": {"address": "SMALL", "reserve_in_usd": "1000"}},
                    {"attributes": {"address": "DEEP", "reserve_in_usd": "250000.5"}},
                    {"attributes": {"reserve_in_usd": "9e9"}},  # no address
                ]
            },
        )
    )
    step = timedelta(minutes=15)
    newest = T + step * 1500
    page1 = [_row(newest - step * i, 1, 1, 1, 1) for i in range(1000)]  # newest first, like the API
    page2 = [_row(newest - step * i, 1, 1, 1, 1) for i in range(999, 1600)]
    route = respx.get(f"{BASE}/networks/solana/pools/DEEP/ohlcv/minute").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": page1}}}),
            httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": page2}}}),
        ]
    )
    client = GeckoTerminalClient(_http(), BASE)
    assert await client.top_pool(MINT) == "DEEP"
    candles = await client.candles("DEEP", MINT, T, newest, 15)
    assert candles[0].ts == T and candles[-1].ts == newest and len(candles) == 1501  # deduplicated, in range
    first, second = (call.request.url.params for call in route.calls)
    assert first["token"] == MINT and first["currency"] == "usd" and first["aggregate"] == "15"
    assert int(second["before_timestamp"]) < int(first["before_timestamp"])


def _history() -> PriceHistory:
    h = PriceHistory(15)
    step = timedelta(minutes=15)
    h.add_candles(
        MINT,
        [
            Candle(T - step, 1.0, 1.0, 1.0, 1.0),
            Candle(T, 1.0, 1.3, 0.7, 1.2),
            Candle(T + step, 1.2, 1.25, 1.1, 1.15),
        ],
    )
    return h


def test_price_at_never_uses_a_candle_that_has_not_closed():
    h = _history()
    assert h.price_at(MINT, T + timedelta(minutes=10)) == 1.0  # the 10:00 candle is still open
    assert h.price_at(MINT, T + timedelta(minutes=15)) == 1.2
    assert h.price_at(MINT, T + timedelta(minutes=31)) == 1.15
    assert h.price_at(MINT, T + timedelta(hours=13)) is None  # too stale
    assert h.price_at("OTHER", T) is None


def test_ohlc_between_and_liquidity_from_snapshots():
    h = _history()
    step = timedelta(minutes=15)
    assert h.ohlc_between(MINT, T, T + step) == (1.0, 1.3, 0.7, 1.2)
    assert h.ohlc_between(MINT, T, T + 2 * step) == (1.0, 1.3, 0.7, 1.15)
    assert h.ohlc_between(MINT, T + timedelta(minutes=5), T + step) is None  # opened mid-candle: skip it
    h.add_snapshot(MINT, T, 1.0, 50_000.0)
    assert h.liquidity_at(MINT, T + timedelta(minutes=30)) == 50_000.0
    assert h.liquidity_at(MINT, T + timedelta(hours=3)) is None


def test_price_needs_ranks_tokens_and_bounds_ranges():
    swaps = {
        "w1": [
            swap("w1", "A", Side.BUY, T, 1, 10),
            swap("w1", "A", Side.SELL, T + timedelta(hours=2), 1, 12),
            swap("w1", "B", Side.BUY, T + timedelta(hours=1), 1, 10),
        ],
        "w2": [
            swap("w2", "A", Side.BUY, T + timedelta(hours=3), 1, 10),
            swap("w2", "C", Side.BUY, T - timedelta(days=9), 1, 10),
        ],
    }
    needs, total = price_needs(swaps, T - timedelta(hours=1), T + timedelta(days=1), T + timedelta(days=2), limit=1)
    assert total == 2  # C was bought before the evaluated period
    assert [n.mint for n in needs] == ["A"] and needs[0].buys == 2
    assert needs[0].start == T - timedelta(days=1)  # 24 h before the first buy (volatility)
    assert needs[0].end == T + timedelta(days=2)  # capped at "now"


def _pos(entry: float = 1.0, peak: float = 1.0) -> _Pos:
    return _Pos(MINT, "w", qty=10, cost=10, entry_price=entry, peak=peak, opened_at=T, at_risk=2)


def test_candle_exit_assumes_the_worst_order():
    cfg = build_config({}).exits  # stop 20 %, TP +50 % (sell half), trailing 15 % from +20 %
    when = T + timedelta(minutes=15)
    d, fill = candle_exit(_pos(), (1.0, 1.1, 0.75, 1.05), when, cfg, ExitMode.PROTECTED)
    assert d.trigger == "stop_loss" and fill == pytest.approx(0.8)  # closed above the stop, but touched it
    d, fill = candle_exit(_pos(), (0.7, 0.9, 0.6, 0.85), when, cfg, ExitMode.PROTECTED)
    assert fill == pytest.approx(0.7)  # gapped through the stop: filled at the open
    d, fill = candle_exit(_pos(), (0.9, 1.6, 0.75, 1.5), when, cfg, ExitMode.PROTECTED)
    assert d.trigger == "stop_loss"  # both the stop and the take profit inside: the stop first
    d, fill = candle_exit(_pos(), (1.0, 1.6, 0.95, 1.4), when, cfg, ExitMode.PROTECTED)
    assert d.tp_level == 0 and fill == pytest.approx(1.5) and d.fraction == 0.5
    assert candle_exit(_pos(), (1.0, 1.1, 0.95, 1.05), when, cfg, ExitMode.PROTECTED) is None
    late = candle_exit(_pos(), (1.0, 1.1, 0.95, 1.05), T + timedelta(days=2), cfg, ExitMode.PROTECTED)
    assert late[0].trigger == "max_hold" and late[1] == 1.05
    # mirror mode: only the emergency stop, filled at its level
    d, fill = candle_exit(_pos(), (1.0, 1.0, 0.4, 0.9), when, cfg, ExitMode.MIRROR)
    assert d.trigger == "emergency_stop" and fill == pytest.approx(0.5)
