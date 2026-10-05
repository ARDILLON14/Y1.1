import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from copytrader.core.clock import ManualClock
from copytrader.core.errors import ProviderError, RateLimitedError
from copytrader.providers.dexscreener import aggregate_pairs
from copytrader.providers.jupiter import parse_quote
from copytrader.providers.rugcheck import parse_rugcheck
from copytrader.providers.solana.mint import parse_mint_account
from copytrader.providers.solana.ws import LogsSubscribeStream, StreamSettings
from copytrader.resilience.circuit_breaker import CircuitBreaker
from copytrader.resilience.http import ResilientHttp
from copytrader.resilience.retry import RetryPolicy

MINT = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"


def test_parse_jupiter_quote():
    q = parse_quote(
        {
            "inputMint": "So11111111111111111111111111111111111111112",
            "inAmount": "100000000",
            "outputMint": MINT,
            "outAmount": "16198753",
            "otherAmountThreshold": "16117760",
            "slippageBps": 50,
            "priceImpactPct": "0.0123",
            "routePlan": [{"swapInfo": {"label": "Meteora DLMM"}, "percent": 100}],
        },
        ManualClock(),
    )
    assert q.out_amount_raw == 16198753 and q.min_out_amount_raw == 16117760
    assert q.price_impact_frac == pytest.approx(0.0123) and q.route_label == "Meteora DLMM"
    with pytest.raises(ProviderError):
        parse_quote({"error": "no route"}, ManualClock())


def test_dexscreener_picks_deepest_base_pair_and_oldest_age():
    pairs = [
        {
            "baseToken": {"address": MINT, "symbol": "TKN"},
            "priceUsd": "1.0",
            "liquidity": {"usd": 5000},
            "pairCreatedAt": 1_700_000_000_000,
            "marketCap": 1e6,
        },
        {
            "baseToken": {"address": MINT, "symbol": "TKN"},
            "priceUsd": "1.1",
            "liquidity": {"usd": 90000},
            "pairCreatedAt": 1_710_000_000_000,
            "marketCap": 1.1e6,
            "priceChange": {"h1": 5, "h24": -10},
        },
        {
            "baseToken": {"address": "OTHER"},
            "quoteToken": {"address": MINT},
            "priceUsd": "99",
            "liquidity": {"usd": 1e9},
        },
    ]
    md = aggregate_pairs(pairs, {MINT})[MINT]
    assert md.price_usd == 1.1 and md.liquidity_usd == 90000
    assert md.pair_created_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    assert md.price_change_pct == {"h1": 5.0, "h24": -10.0}


def test_rugcheck_levels():
    safe = parse_rugcheck(MINT, {"score_normalised": 5, "risks": [{"name": "Low liquidity", "level": "warn"}]})
    assert safe.level == "medium" and safe.score == 5 and not safe.is_rugged
    rug = parse_rugcheck(MINT, {"score": 5000, "rugged": True, "risks": []})
    assert rug.is_rugged and rug.level == "critical" and rug.score == 100
    danger = parse_rugcheck(MINT, {"score_normalised": 20, "risks": [{"name": "Freeze Authority", "level": "danger"}]})
    assert danger.level == "high"


def test_mint_account_dangerous_extensions():
    value = {
        "owner": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        "data": {
            "parsed": {
                "info": {
                    "decimals": 6,
                    "mintAuthority": None,
                    "freezeAuthority": "Frz111",
                    "extensions": [
                        {
                            "extension": "transferFeeConfig",
                            "state": {"newerTransferFee": {"transferFeeBasisPoints": 100}},
                        },
                        {"extension": "permanentDelegate", "state": {"delegate": "X"}},
                        {"extension": "metadataPointer", "state": {}},
                    ],
                }
            }
        },
    }
    md = parse_mint_account(MINT, value)
    assert md.freeze_authority == "Frz111" and md.mint_authority is None
    assert set(md.dangerous_extensions) == {"transferFee", "permanentDelegate"}


def _http(**kw):
    return ResilientHttp(
        "t",
        timeout=1,
        rate_per_second=1000,
        retry=RetryPolicy(3, 0.001, 0.002),
        breaker=CircuitBreaker("t", 5, 10),
        **kw,
    )


@respx.mock
async def test_resilient_http_retries_5xx_then_succeeds():
    route = respx.get("https://x.test/a").mock(side_effect=[httpx.Response(502), httpx.Response(200, json={"ok": 1})])
    http = _http()
    assert await http.get_json("https://x.test/a") == {"ok": 1}
    assert route.call_count == 2
    await http.aclose()


@respx.mock
async def test_resilient_http_does_not_retry_4xx_and_maps_429():
    respx.get("https://x.test/b").mock(return_value=httpx.Response(400, text="bad"))
    respx.get("https://x.test/c").mock(return_value=httpx.Response(429, headers={"retry-after": "0"}))
    http = _http()
    with pytest.raises(ProviderError) as e:
        await http.get_json("https://x.test/b")
    assert e.value.status_code == 400 and not e.value.retryable
    with pytest.raises(RateLimitedError):
        await http.get_json("https://x.test/c")
    await http.aclose()


# ------------------------------------------------------------- websocket
class FakeWS:
    def __init__(self, server):
        self.server = server
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw):
        msg = json.loads(raw)
        self.server.sent.append(msg)
        if msg["method"] == "logsSubscribe":
            await self.inbox.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": 100 + msg["id"]}))

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item


class FakeServer:
    def __init__(self):
        self.sent = []
        self.connections: list[FakeWS] = []

    def connect(self, url, **kw):
        ws = FakeWS(self)
        self.connections.append(ws)
        return ws


async def test_logs_subscribe_stream_subscribes_notifies_and_reconnects():
    from websockets.exceptions import ConnectionClosedError

    server = FakeServer()
    notices = []
    reconnects = []

    async def on_reconnect():
        reconnects.append(1)

    stream = LogsSubscribeStream(
        StreamSettings(url="wss://x", max_backoff=0.01), connect=server.connect, on_reconnect=on_reconnect
    )
    stream.set_wallets({"W1", "W2"})

    async def sink(n):
        notices.append(n)

    task = asyncio.create_task(stream.run(sink))
    await asyncio.sleep(0.05)
    subs = [m for m in server.sent if m["method"] == "logsSubscribe"]
    assert {m["params"][0]["mentions"][0] for m in subs} == {"W1", "W2"}
    ws = server.connections[0]
    sub_w1 = next(100 + m["id"] for m in subs if m["params"][0]["mentions"][0] == "W1")
    await ws.inbox.put(
        json.dumps(
            {
                "method": "logsNotification",
                "params": {
                    "subscription": sub_w1,
                    "result": {"context": {"slot": 7}, "value": {"signature": "SIG1", "err": None}},
                },
            }
        )
    )
    await ws.inbox.put(
        json.dumps(
            {
                "method": "logsNotification",
                "params": {
                    "subscription": sub_w1,
                    "result": {"context": {"slot": 8}, "value": {"signature": "FAILED", "err": {"x": 1}}},
                },
            }
        )
    )
    await asyncio.sleep(0.05)
    assert [(n.signature, n.wallet, n.slot) for n in notices] == [("SIG1", "W1", 7)]
    # drop the connection: the stream reconnects, resubscribes and triggers catch-up
    await ws.inbox.put(ConnectionClosedError(None, None))
    await asyncio.sleep(0.2)
    assert len(server.connections) >= 2 and reconnects
    assert len([m for m in server.sent if m["method"] == "logsSubscribe"]) >= 4
    # dynamic unsubscribe
    stream.set_wallets({"W1"})
    await asyncio.sleep(0.05)
    assert any(m["method"] == "logsUnsubscribe" for m in server.sent)
    await stream.stop()
    await asyncio.wait_for(task, 2)


@respx.mock
async def test_sol_price_history_does_not_repeat_a_request_that_just_failed():
    """1,000 transactions waiting on the same failing chunk must not each repeat the slow request."""
    from copytrader.providers.prices import KlinesSolPriceHistory

    route = respx.get("https://k.test/klines").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=[[1_750_000_000_000, "1", "1", "1", "150.5", "1"]])]
    )
    http = ResilientHttp(
        "sol_price_history",
        timeout=1,
        rate_per_second=1000,
        retry=RetryPolicy(1, 0.001, 0.001),
        breaker=CircuitBreaker("s", 50, 1),
    )
    prices = KlinesSolPriceHistory(http, "https://k.test/klines")
    when = datetime.fromtimestamp(1_750_000_000, tz=UTC)
    with pytest.raises(ProviderError):
        await prices.sol_price_at(when)
    with pytest.raises(ProviderError, match="falló hace un momento"):
        await prices.sol_price_at(when)
    assert route.call_count == 1
    prices._failed_at.clear()  # the pause is over
    assert await prices.sol_price_at(when) == 150.5
    assert route.call_count == 2
