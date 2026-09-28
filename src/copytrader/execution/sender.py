"""Send a signed transaction through several routes at once.

A transaction lands sooner (and more often) when more paths reach the current
leader. Every configured route gets the same signed bytes in parallel — the
signature is the same everywhere, so it can only execute once:

* the main RPC (always, unless ``execution.jito_only``);
* extra RPC endpoints (``execution.extra_send_urls`` + secret
  ``SOLANA_SEND_RPC_URLS``), e.g. a staked "sender" endpoint;
* Jito's block engine (``execution.send_via_jito``), only for transactions
  that carry a tip. With ``execution.jito_only`` it is the ONLY route and the
  transaction is sent bundle-only: nobody can sandwich it, but it only lands
  when a Jito validator is the leader. Protective exits (urgent fee decisions)
  ignore ``jito_only`` and use every route: they must land.

``send`` returns as soon as one route accepts; the others finish in the
background. It fails only if every route refused. Confirmation is still read
from the chain by the executor: "accepted" never means "executed".
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import structlog

from copytrader.config.models import AppConfig
from copytrader.core.errors import CopyTraderError, ExecutionError, ProviderError
from copytrader.observability import metrics
from copytrader.observability.speed import SpeedStats
from copytrader.resilience.http import ResilientHttp
from copytrader.resilience.rate_limiter import Priority
from copytrader.resilience.retry import RetryPolicy

log = structlog.get_logger(__name__)

_NO_RETRY = RetryPolicy(max_attempts=1)  # the executor's rebroadcast loop is the retry


class RawSender(Protocol):
    async def send_raw_transaction(self, tx_bytes: bytes, *, skip_preflight: bool = True) -> str: ...


class JitoBlockEngine:
    """Jito ``sendTransaction`` (JSON-RPC over HTTP) on one block-engine endpoint."""

    def __init__(self, http: ResilientHttp, url: str, *, auth: str | None = None) -> None:
        self.http = http
        self.url = url.rstrip("/")
        self._headers = {"x-jito-auth": auth} if auth else None

    async def send(self, tx_bytes: bytes, *, bundle_only: bool = False) -> str:
        url = f"{self.url}?bundleOnly=true" if bundle_only else self.url
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [base64.b64encode(tx_bytes).decode(), {"encoding": "base64"}],
        }
        data = await self.http.post_json(
            url, json=payload, headers=self._headers, retry=_NO_RETRY, priority=Priority.EXECUTION
        )
        if not isinstance(data, dict) or data.get("error") or not data.get("result"):
            err = data.get("error") if isinstance(data, dict) else data
            raise ProviderError(f"jito: {err}", provider="jito", retryable=True)
        return str(data["result"])


class TransactionSender:
    def __init__(
        self,
        config: Callable[[], AppConfig],
        rpc: RawSender,
        *,
        extra_rpcs: Sequence[RawSender] = (),
        jito: Sequence[JitoBlockEngine] = (),
        speed: SpeedStats | None = None,
    ) -> None:
        self._config = config
        self.rpc = rpc
        self.extra_rpcs = list(extra_rpcs)
        self.jito = list(jito)
        self.speed = speed
        self._bg: set[asyncio.Future[bool]] = set()

    def routes(self, *, tipped: bool, urgent: bool = False) -> list[str]:
        """Names of the routes a transaction would take (for logs and the dashboard)."""
        return [name for name, _ in self._routes(b"", tipped=tipped, urgent=urgent, skip_preflight=True, build=False)]

    def _routes(
        self, tx_bytes: bytes, *, tipped: bool, urgent: bool, skip_preflight: bool, build: bool = True
    ) -> list[tuple[str, Awaitable[Any] | None]]:
        ex = self._config().execution
        # protective exits are never Jito-only: landing them matters more than sandwich protection
        jito_only = ex.jito_only and tipped and not urgent and bool(self.jito)
        out: list[tuple[str, Awaitable[Any] | None]] = []
        if not jito_only:
            out.append(
                ("rpc", self.rpc.send_raw_transaction(tx_bytes, skip_preflight=skip_preflight) if build else None)
            )
            for i, extra in enumerate(self.extra_rpcs, 1):
                call = extra.send_raw_transaction(tx_bytes, skip_preflight=skip_preflight) if build else None
                out.append((f"rpc_extra_{i}", call))
        if tipped and (ex.send_via_jito or ex.jito_only):
            for i, engine in enumerate(self.jito, 1):
                name = "jito" if len(self.jito) == 1 else f"jito_{i}"
                out.append((name, engine.send(tx_bytes, bundle_only=jito_only) if build else None))
        return out

    async def _one(self, route: str, call: Awaitable[Any]) -> bool:
        started = time.perf_counter()
        try:
            await call
        except CopyTraderError as exc:
            metrics.TX_SEND.labels(route=route, outcome="error").inc()
            if self.speed is not None:
                self.speed.record_send(route, False)
            log.warning("send_route_failed", route=route, error=str(exc)[:200])
            return False
        elapsed = (time.perf_counter() - started) * 1000
        metrics.TX_SEND.labels(route=route, outcome="ok").inc()
        if self.speed is not None:
            self.speed.record_send(route, True, elapsed)
        return True

    async def send(
        self, tx_bytes: bytes, *, tipped: bool = False, urgent: bool = False, skip_preflight: bool = True
    ) -> None:
        """Send through every route; return when one accepts, raise if all refuse."""
        routes = self._routes(tx_bytes, tipped=tipped, urgent=urgent, skip_preflight=skip_preflight)
        pending: set[asyncio.Future[bool]] = {
            asyncio.ensure_future(self._one(name, call)) for name, call in routes if call is not None
        }
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if any(task.result() for task in done):
                for task in pending:  # slower routes keep going on their own
                    self._bg.add(task)
                    task.add_done_callback(self._bg.discard)
                return
        raise ExecutionError(f"ninguna ruta aceptó la transacción ({', '.join(n for n, _ in routes)})", retryable=True)
