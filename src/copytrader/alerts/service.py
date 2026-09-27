"""AlertService: domain events → persisted alerts (+ notifications).

Subscribes to the event bus, so the trading path never waits for it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.events import (
    Event,
    EventBus,
    ExecutionFailed,
    KillSwitchChanged,
    PositionClosed,
    ProviderStatusChanged,
    RiskLimitHit,
    SignalAlert,
    SignalDecided,
    SystemMessage,
    WalletStatusChanged,
)
from copytrader.core.types import AlertType, Severity
from copytrader.db.base import Database
from copytrader.db.models import Alert
from copytrader.db.repositories import AlertRepo
from copytrader.notifications import formatting as fmt
from copytrader.notifications.channels import Notification, NotificationService
from copytrader.security.redaction import REDACTOR

log = structlog.get_logger(__name__)


class AlertService:
    def __init__(self, db: Database, clock: Clock, config: Callable[[], AppConfig],
                 notifier: NotificationService) -> None:
        self.db = db
        self.clock = clock
        self._config = config
        self.notifier = notifier

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(SignalDecided, self._on_signal_decided)
        bus.subscribe(SignalAlert, self._on_signal_alert)
        bus.subscribe(PositionClosed, self._on_position_closed)
        bus.subscribe(ExecutionFailed, self._on_execution_failed)
        bus.subscribe(WalletStatusChanged, self._on_wallet_status)
        bus.subscribe(KillSwitchChanged, self._on_kill_switch)
        bus.subscribe(RiskLimitHit, self._on_risk)
        bus.subscribe(ProviderStatusChanged, self._on_provider)
        bus.subscribe(SystemMessage, self._on_system)

    async def raise_alert(self, type_: AlertType, severity: Severity, title: str, body: str,
                          data: dict[str, Any] | None = None, dedupe_key: str | None = None) -> None:
        title, body = REDACTOR.text(title), REDACTOR.text(body)
        channels: list[str] = []
        cfg = self._config().notifications
        if cfg.events.get(type_, True):
            if self.notifier.submit(Notification(title, body, severity, dedupe_key)):
                channels = [c.name for c in self.notifier.channels]
        try:
            async with self.db.session() as s:
                await AlertRepo(s).add(Alert(ts=self.clock.now(), type=type_.value, severity=severity.value,
                                             title=title[:200], body=body, data=REDACTOR.data(data or {}),
                                             dedupe_key=dedupe_key, channels=channels))
        except Exception:
            log.exception("alert_persist_failed", title=title)

    async def _on_signal_decided(self, ev: SignalDecided) -> None:
        title, body = fmt.format_signal_decided(ev)
        await self.raise_alert(AlertType.TRADE_COPIED if ev.approved else AlertType.TRADE_REJECTED,
                               Severity.INFO, title, body,
                               {**fmt.as_dict(ev), "explanation": ev.explanation}, f"sig:{ev.signal_id}")

    async def _on_signal_alert(self, ev: SignalAlert) -> None:
        title, body = fmt.format_signal_alert(ev)
        await self.raise_alert(AlertType.SIGNAL_DETECTED, Severity.INFO, title, body, fmt.as_dict(ev))

    async def _on_position_closed(self, ev: PositionClosed) -> None:
        title, body = fmt.format_position_closed(ev)
        await self.raise_alert(AlertType.POSITION_CLOSED, Severity.INFO, title, body, fmt.as_dict(ev),
                               f"pos:{ev.position_id}")

    async def _on_execution_failed(self, ev: ExecutionFailed) -> None:
        title, body = fmt.format_execution_failed(ev)
        await self.raise_alert(AlertType.EXECUTION_ERROR, Severity.WARNING, title, body, fmt.as_dict(ev),
                               f"exec:{ev.client_order_id}")

    async def _on_wallet_status(self, ev: WalletStatusChanged) -> None:
        title, body = fmt.format_wallet_status(ev)
        await self.raise_alert(AlertType.WALLET_DEGRADED if ev.degraded else AlertType.WALLET_STATUS,
                               Severity.WARNING if ev.degraded else Severity.INFO, title, body, fmt.as_dict(ev))

    async def _on_kill_switch(self, ev: KillSwitchChanged) -> None:
        title, body = fmt.format_kill_switch(ev)
        await self.raise_alert(AlertType.KILL_SWITCH, Severity.CRITICAL if ev.active else Severity.INFO,
                               title, body, fmt.as_dict(ev))

    async def _on_risk(self, ev: RiskLimitHit) -> None:
        title, body = fmt.format_risk(ev)
        await self.raise_alert(AlertType.RISK_EXCEEDED, Severity.CRITICAL, title, body, fmt.as_dict(ev),
                               f"risk:{ev.limit}")

    async def _on_provider(self, ev: ProviderStatusChanged) -> None:
        title, body = fmt.format_provider(ev)
        type_ = AlertType.RPC_PROBLEM if ev.kind == "rpc" else AlertType.API_DISCONNECTED
        await self.raise_alert(type_, Severity.INFO if ev.healthy else Severity.WARNING, title, body,
                               fmt.as_dict(ev), f"prov:{ev.provider}:{ev.healthy}")

    async def _on_system(self, ev: SystemMessage) -> None:
        title, body = fmt.format_system(ev)
        await self.raise_alert(AlertType.SYSTEM, ev.severity, title, body, fmt.as_dict(ev))


__all__ = ["AlertService", "Event"]
