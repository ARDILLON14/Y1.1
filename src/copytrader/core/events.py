"""In-process event bus for side effects (alerts, notifications, metrics).

The critical path (detection → risk → execution) never waits on subscribers:
``publish`` schedules handlers as background tasks and isolates their errors.
If the application is ever split into services, this class can be replaced by
Redis Streams / NATS behind the same two methods.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

import structlog

from copytrader.core.clock import utcnow
from copytrader.core.types import KillSwitchScope, Severity

log = structlog.get_logger(__name__)


@dataclass(frozen=True, kw_only=True)
class Event:
    ts: datetime = field(default_factory=utcnow)
    trace_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class SignalDecided(Event):
    signal_id: int
    wallet: str
    wallet_label: str | None
    wallet_score: float | None
    token_mint: str
    token_symbol: str | None
    side: str
    action: str
    approved: bool
    reason: str | None
    explanation: str
    source_price_usd: float | None
    liquidity_usd: float | None
    size_usd: float | None
    mode: str | None
    checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class SignalAlert(Event):
    """A trade detected on a watch-only wallet (no copy attempted)."""

    wallet: str
    wallet_label: str | None
    wallet_score: float | None
    token_mint: str
    token_symbol: str | None
    side: str
    price_usd: float | None
    value_usd: float | None
    reason: str


@dataclass(frozen=True, kw_only=True)
class TradeExecuted(Event):
    mode: str
    purpose: str
    side: str
    token_mint: str
    token_symbol: str | None
    value_usd: float
    fill_price_usd: float | None
    slippage_bps: float | None
    fees_usd: float
    tx_signature: str | None
    position_id: int | None
    source_wallet: str | None


@dataclass(frozen=True, kw_only=True)
class ExecutionFailed(Event):
    mode: str
    purpose: str
    token_mint: str
    error: str
    client_order_id: str


@dataclass(frozen=True, kw_only=True)
class PositionClosed(Event):
    position_id: int
    mode: str
    token_mint: str
    token_symbol: str | None
    reason: str
    realized_pnl_usd: float
    return_pct: float | None


@dataclass(frozen=True, kw_only=True)
class KillSwitchChanged(Event):
    scope: KillSwitchScope
    active: bool
    reason: str
    actor: str


@dataclass(frozen=True, kw_only=True)
class RiskLimitHit(Event):
    limit: str
    message: str
    value: float | None = None
    threshold: float | None = None


@dataclass(frozen=True, kw_only=True)
class WalletStatusChanged(Event):
    wallet: str
    wallet_label: str | None
    old_status: str | None
    new_status: str
    reasons: list[str]
    degraded: bool = False


@dataclass(frozen=True, kw_only=True)
class ProviderStatusChanged(Event):
    provider: str
    kind: str  # "websocket" | "rpc" | "http" | "signer"
    healthy: bool
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class SystemMessage(Event):
    title: str
    body: str
    severity: Severity = Severity.INFO


E = TypeVar("E", bound=Event)
Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[Event], list[Handler]] = defaultdict(list)
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, event_type: type[E], handler: Callable[[E], Awaitable[None]]) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        """Schedule all handlers for ``event`` (and its base classes)."""
        for etype in type(event).__mro__:
            for handler in self._handlers.get(etype, ()):  # type: ignore[call-overload]
                task = asyncio.get_running_loop().create_task(self._run(handler, event))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _run(self, handler: Handler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:  # isolate subscriber failures from the publisher
            log.exception("event_handler_failed", event=type(event).__name__,
                          handler=getattr(handler, "__qualname__", repr(handler)))

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait for in-flight handlers (used on shutdown and in tests)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._tasks:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.wait(set(self._tasks), timeout=remaining)
