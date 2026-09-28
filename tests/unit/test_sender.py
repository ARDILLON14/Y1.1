"""Multi-route transaction sending: RPC, extra RPCs and Jito, first acceptance wins."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
import respx

from copytrader.config.loader import build_config
from copytrader.core.errors import ExecutionError, ProviderError
from copytrader.execution.sender import JitoBlockEngine, TransactionSender
from copytrader.observability.speed import SpeedStats
from copytrader.resilience.circuit_breaker import CircuitBreaker
from copytrader.resilience.http import ResilientHttp
from copytrader.resilience.retry import RetryPolicy

JITO_URL = "https://jito.test/api/v1/transactions"


class FakeRoute:
    def __init__(self, name: str, delay: float = 0.0, fail: bool = False) -> None:
        self.name = name
        self.delay = delay
        self.fail = fail
        self.sent: list[tuple[bytes, dict]] = []

    async def send_raw_transaction(self, tx_bytes: bytes, *, skip_preflight: bool = True) -> str:
        return await self._send(tx_bytes, skip_preflight=skip_preflight)

    async def send(self, tx_bytes: bytes, *, bundle_only: bool = False) -> str:
        return await self._send(tx_bytes, bundle_only=bundle_only)

    async def _send(self, tx_bytes: bytes, **kw) -> str:
        await asyncio.sleep(self.delay)
        self.sent.append((tx_bytes, kw))
        if self.fail:
            raise ProviderError(f"{self.name} down", provider=self.name, retryable=True)
        return "SIG"


def sender(execution: dict | None = None, **routes) -> TransactionSender:
    c = build_config({"execution": {"jito_tip_lamports": 100_000, **(execution or {})}})
    return TransactionSender(
        lambda: c,
        routes["rpc"],
        extra_rpcs=routes.get("extra", []),
        jito=routes.get("jito", []),
        speed=routes.get("speed"),
    )


async def test_routes_depend_on_tip_and_settings():
    rpc, extra, jito = FakeRoute("rpc"), FakeRoute("extra"), FakeRoute("jito")
    s = sender({"send_via_jito": True}, rpc=rpc, extra=[extra], jito=[jito])
    assert s.routes(tipped=True) == ["rpc", "rpc_extra_1", "jito"]
    assert s.routes(tipped=False) == ["rpc", "rpc_extra_1"]  # Jito only takes tipped transactions
    only = sender({"jito_only": True}, rpc=rpc, extra=[extra], jito=[jito])
    assert only.routes(tipped=True) == ["jito"]  # bundle-only: never through public RPCs
    assert only.routes(tipped=False) == ["rpc", "rpc_extra_1"]  # e.g. closing token accounts
    # a protective exit must land: every route, Jito included (not bundle-only)
    assert only.routes(tipped=True, urgent=True) == ["rpc", "rpc_extra_1", "jito"]
    assert sender(rpc=rpc, jito=[jito]).routes(tipped=True) == ["rpc"]  # Jito sending is opt-in


async def test_first_acceptance_returns_and_slow_routes_finish_in_background():
    speed = SpeedStats()
    rpc, slow = FakeRoute("rpc"), FakeRoute("jito", delay=0.2)
    s = sender({"send_via_jito": True}, rpc=rpc, jito=[slow], speed=speed)
    started = asyncio.get_running_loop().time()
    await s.send(b"tx", tipped=True)
    assert asyncio.get_running_loop().time() - started < 0.15
    assert rpc.sent and not slow.sent
    await asyncio.sleep(0.3)
    assert slow.sent == [(b"tx", {"bundle_only": False})]
    routes = {r["route"]: r for r in speed.snapshot()["send_routes"]}
    assert routes["rpc"]["accepted"] == 1 and routes["jito"]["accepted"] == 1


async def test_one_route_down_is_not_a_failure_all_down_is():
    s = sender({"send_via_jito": True}, rpc=FakeRoute("rpc", fail=True), jito=[FakeRoute("jito")])
    await s.send(b"tx", tipped=True)
    dead = sender({"send_via_jito": True}, rpc=FakeRoute("rpc", fail=True), jito=[FakeRoute("jito", fail=True)])
    with pytest.raises(ExecutionError) as exc:
        await dead.send(b"tx", tipped=True)
    assert exc.value.retryable and "rpc" in str(exc.value) and "jito" in str(exc.value)


async def test_jito_only_sends_bundle_only():
    rpc, jito = FakeRoute("rpc"), FakeRoute("jito")
    await sender({"jito_only": True}, rpc=rpc, jito=[jito]).send(b"tx", tipped=True)
    assert not rpc.sent and jito.sent == [(b"tx", {"bundle_only": True})]


def _http() -> ResilientHttp:
    return ResilientHttp(
        "jito", timeout=1, rate_per_second=100, retry=RetryPolicy(1, 0.001, 0.001), breaker=CircuitBreaker("j", 5, 1)
    )


@respx.mock
async def test_jito_block_engine_request():
    route = respx.post(JITO_URL).mock(return_value=httpx.Response(200, json={"jsonrpc": "2.0", "result": "SIG1"}))
    engine = JitoBlockEngine(_http(), JITO_URL, auth="uuid-1")
    assert await engine.send(b"\x01\x02") == "SIG1"
    body = json.loads(route.calls[0].request.content)
    assert body["method"] == "sendTransaction"
    assert body["params"] == [base64.b64encode(b"\x01\x02").decode(), {"encoding": "base64"}]
    assert route.calls[0].request.headers["x-jito-auth"] == "uuid-1"
    assert "bundleOnly" not in str(route.calls[0].request.url)
    await engine.send(b"\x01", bundle_only=True)
    assert route.calls[1].request.url.params["bundleOnly"] == "true"


@respx.mock
async def test_jito_error_is_a_provider_error():
    respx.post(JITO_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "error": {"code": -32602, "message": "tip too low"}})
    )
    with pytest.raises(ProviderError):
        await JitoBlockEngine(_http(), JITO_URL).send(b"\x01")
