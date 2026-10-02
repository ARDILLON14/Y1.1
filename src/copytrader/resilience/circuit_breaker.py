"""Circuit breaker: stop hammering a failing dependency and fail fast instead.

States: CLOSED (normal) → OPEN after ``failure_threshold`` consecutive failures
→ HALF_OPEN after ``reset_timeout`` (one trial call) → CLOSED on success or
OPEN again on failure.

A 429 (rate limited) proves the dependency is up: it only asks us to slow
down, and the rate limiter + ``retry-after`` already do that. It counts as
alive (closes a half-open breaker) instead of as a failure, which would open
the breaker and turn a short throttle into every pending call failing at once.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

import structlog

from copytrader.core.errors import CircuitOpenError, ProviderError, RateLimitedError

log = structlog.get_logger(__name__)
T = TypeVar("T")


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


StateListener = Callable[[str, BreakerState, BreakerState], None]


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: StateListener | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = False
        self._on_state_change = on_state_change

    @property
    def state(self) -> BreakerState:
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self.reset_timeout:
            self._transition(BreakerState.HALF_OPEN)
        return self._state

    @property
    def is_available(self) -> bool:
        return self.state is not BreakerState.OPEN

    def _transition(self, new: BreakerState) -> None:
        old = self._state
        if old is new:
            return
        self._state = new
        if new is BreakerState.OPEN:
            self._opened_at = self._clock()
        if new is not BreakerState.HALF_OPEN:
            self._half_open_in_flight = False
        log.warning("circuit_state", breaker=self.name, old=old.value, new=new.value)
        if self._on_state_change:
            self._on_state_change(self.name, old, new)

    def record_success(self) -> None:
        self._failures = 0
        self._half_open_in_flight = False
        if self._state is not BreakerState.CLOSED:
            self._transition(BreakerState.CLOSED)

    def record_failure(self) -> None:
        self._failures += 1
        self._half_open_in_flight = False
        if self._state is BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
            self._transition(BreakerState.OPEN)

    def _before_call(self) -> None:
        state = self.state
        if state is BreakerState.OPEN:
            raise CircuitOpenError(self.name)
        if state is BreakerState.HALF_OPEN:
            if self._half_open_in_flight:
                raise CircuitOpenError(self.name)
            self._half_open_in_flight = True

    async def call(
        self, fn: Callable[[], Awaitable[T]], counts_as_failure: Callable[[BaseException], bool] | None = None
    ) -> T:
        self._before_call()
        try:
            result = await fn()
        except RateLimitedError:
            self.record_success()  # throttled, not down
            raise
        except Exception as exc:
            failure = counts_as_failure(exc) if counts_as_failure else _default_failure(exc)
            if failure:
                self.record_failure()
            else:
                # e.g. a 4xx "bad request" proves the dependency is alive
                self.record_success()
            raise
        self.record_success()
        return result


def _default_failure(exc: BaseException) -> bool:
    if isinstance(exc, ProviderError):
        return exc.retryable
    return True
