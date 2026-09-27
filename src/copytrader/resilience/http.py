"""HTTP client wrapper combining timeout + rate limit + circuit breaker + retry.

Every external HTTP dependency goes through one ``ResilientHttp`` instance so
failure handling is uniform and observable (metrics + health registry).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from copytrader.core.errors import ProviderError, RateLimitedError
from copytrader.observability import metrics
from copytrader.observability.health import HealthRegistry, HealthStatus
from copytrader.resilience.circuit_breaker import CircuitBreaker
from copytrader.resilience.rate_limiter import TokenBucket
from copytrader.resilience.retry import RetryPolicy, retry_async

log = structlog.get_logger(__name__)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class ResilientHttp:
    def __init__(
        self,
        name: str,
        *,
        timeout: float,
        rate_per_second: float,
        retry: RetryPolicy,
        breaker: CircuitBreaker,
        health: HealthRegistry | None = None,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.retry = retry
        self.breaker = breaker
        self.bucket = TokenBucket(rate_per_second, capacity=max(1.0, rate_per_second))
        self.health = health
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            headers={"User-Agent": "copytrader/0.1", **(headers or {})},
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retry: RetryPolicy | None = None,
    ) -> Any:
        return await self.request_json("GET", url, params=params, headers=headers, retry=retry)

    async def post_json(
        self, url: str, *, json: Any = None, headers: dict[str, str] | None = None, retry: RetryPolicy | None = None
    ) -> Any:
        return await self.request_json("POST", url, json=json, headers=headers, retry=retry)

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        retry: RetryPolicy | None = None,
    ) -> Any:
        async def attempt() -> Any:
            return await self.breaker.call(lambda: self._once(method, url, params, json, headers))

        try:
            result = await retry_async(attempt, retry or self.retry, name=f"{self.name}:{method}")
        except ProviderError as exc:
            if self.health is not None and exc.retryable:
                self.health.fail(self.name, "http", str(exc), status=HealthStatus.DEGRADED)
            raise
        if self.health is not None:
            self.health.ok(self.name, "http")
        return result

    async def _once(
        self, method: str, url: str, params: dict[str, Any] | None, json: Any, headers: dict[str, str] | None
    ) -> Any:
        await self.bucket.acquire()
        started = time.perf_counter()
        outcome = "error"
        try:
            try:
                response = await self._client.request(method, url, params=params, json=json, headers=headers)
            except httpx.TimeoutException as exc:
                outcome = "timeout"
                raise ProviderError(f"{self.name}: timeout", provider=self.name) from exc
            except httpx.TransportError as exc:
                outcome = "transport"
                raise ProviderError(f"{self.name}: {type(exc).__name__}", provider=self.name) from exc
            status = response.status_code
            if status == 429:
                outcome = "rate_limited"
                retry_after = _retry_after(response)
                self.bucket.penalize(retry_after or 1.0)
                raise RateLimitedError(f"{self.name}: rate limited", provider=self.name, retry_after=retry_after)
            if status >= 500:
                outcome = f"http_{status}"
                raise ProviderError(f"{self.name}: HTTP {status}", provider=self.name, status_code=status)
            if status >= 400:
                outcome = f"http_{status}"
                detail = response.text[:300]
                raise ProviderError(
                    f"{self.name}: HTTP {status}: {detail}", provider=self.name, retryable=False, status_code=status
                )
            try:
                data = response.json()
            except ValueError as exc:
                outcome = "bad_json"
                raise ProviderError(f"{self.name}: invalid JSON", provider=self.name) from exc
            outcome = "ok"
            return data
        finally:
            metrics.PROVIDER_LATENCY.labels(provider=self.name).observe(time.perf_counter() - started)
            metrics.PROVIDER_REQUESTS.labels(provider=self.name, outcome=outcome).inc()
