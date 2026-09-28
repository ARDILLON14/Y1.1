import asyncio

import pytest

from copytrader.core.errors import CircuitOpenError, ProviderError, RateLimitedError
from copytrader.resilience.circuit_breaker import BreakerState, CircuitBreaker
from copytrader.resilience.rate_limiter import Priority, SlidingWindowCounter, TokenBucket
from copytrader.resilience.retry import RetryPolicy, retry_async


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def _no_sleep(_: float) -> None:
    return None


async def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("boom", retryable=True)
        return "ok"

    result = await retry_async(flaky, RetryPolicy(max_attempts=5), sleep=_no_sleep)
    assert result == "ok"
    assert calls["n"] == 3


async def test_retry_does_not_retry_non_retryable():
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        raise ProviderError("400", retryable=False)

    with pytest.raises(ProviderError):
        await retry_async(bad, RetryPolicy(max_attempts=5), sleep=_no_sleep)
    assert calls["n"] == 1


async def test_retry_gives_up_after_max_attempts():
    calls = {"n": 0}

    async def always():
        calls["n"] += 1
        raise RateLimitedError("429", retry_after=0.01)

    with pytest.raises(RateLimitedError):
        await retry_async(always, RetryPolicy(max_attempts=3), sleep=_no_sleep)
    assert calls["n"] == 3


def test_backoff_is_bounded():
    policy = RetryPolicy(max_attempts=10, base_delay=0.5, max_delay=2.0)
    for attempt in range(10):
        assert 0 <= policy.delay_for(attempt) <= 2.0


async def test_circuit_breaker_opens_and_recovers():
    clock = FakeTime()
    cb = CircuitBreaker("x", failure_threshold=2, reset_timeout=10, clock=clock)

    async def fail():
        raise ProviderError("down")

    async def ok():
        return 1

    for _ in range(2):
        with pytest.raises(ProviderError):
            await cb.call(fail)
    assert cb.state is BreakerState.OPEN
    with pytest.raises(CircuitOpenError):
        await cb.call(ok)
    clock.t = 11
    assert cb.state is BreakerState.HALF_OPEN
    assert await cb.call(ok) == 1
    assert cb.state is BreakerState.CLOSED


async def test_circuit_half_open_failure_reopens():
    clock = FakeTime()
    cb = CircuitBreaker("x", failure_threshold=1, reset_timeout=5, clock=clock)

    async def fail():
        raise ProviderError("down")

    with pytest.raises(ProviderError):
        await cb.call(fail)
    clock.t = 6
    with pytest.raises(ProviderError):
        await cb.call(fail)
    assert cb.state is BreakerState.OPEN


async def test_client_errors_do_not_open_circuit():
    cb = CircuitBreaker("x", failure_threshold=1)

    async def bad_request():
        raise ProviderError("400", retryable=False)

    for _ in range(3):
        with pytest.raises(ProviderError):
            await cb.call(bad_request)
    assert cb.state is BreakerState.CLOSED


async def test_token_bucket_limits_rate():
    bucket = TokenBucket(rate=100, capacity=2)
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert not bucket.try_acquire()
    await asyncio.wait_for(bucket.acquire(), timeout=1)


async def test_token_bucket_serves_order_execution_before_background_polling():
    bucket = TokenBucket(rate=20, capacity=1)
    await bucket.acquire()  # budget exhausted
    served: list[str] = []

    async def take(name: str, priority: Priority) -> None:
        await bucket.acquire(priority=priority)
        served.append(name)

    background = [asyncio.create_task(take(f"price{i}", Priority.BACKGROUND)) for i in range(3)]
    await asyncio.sleep(0)  # the price polls queue up first...
    execution = asyncio.create_task(take("quote", Priority.EXECUTION))  # ...then an order needs a quote
    await asyncio.wait_for(asyncio.gather(*background, execution), timeout=3)
    assert served[0] == "quote"
    assert sorted(served[1:]) == ["price0", "price1", "price2"]


def test_sliding_window_counter():
    clock = FakeTime()
    c = SlidingWindowCounter(60, clock=clock)
    c.add()
    c.add()
    clock.t = 30
    assert c.add() == 3
    clock.t = 61
    assert c.count() == 1
