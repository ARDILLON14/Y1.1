"""Retry with exponential backoff and full jitter."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import structlog

from copytrader.core.errors import ProviderError, RateLimitedError

log = structlog.get_logger(__name__)
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 3.0

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Full-jitter backoff: uniform(0, min(max, base * 2**attempt))."""
        cap = min(self.max_delay, self.base_delay * (2**attempt))
        return (rng or random).uniform(0, cap)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ProviderError):
        return exc.retryable
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    name: str = "operation",
    retry_on: Callable[[BaseException], bool] = is_retryable,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Call ``fn`` until it succeeds, a non-retryable error occurs or attempts run out."""
    last_exc: BaseException | None = None
    for attempt in range(policy.max_attempts):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            if not retry_on(exc) or attempt == policy.max_attempts - 1:
                raise
            delay = policy.delay_for(attempt)
            if isinstance(exc, RateLimitedError) and exc.retry_after:
                delay = max(delay, min(exc.retry_after, policy.max_delay * 4))
            log.debug("retrying", op=name, attempt=attempt + 1, delay=round(delay, 3), error=type(exc).__name__)
            await sleep(delay)
    assert last_exc is not None  # pragma: no cover - loop always returns or raises
    raise last_exc
