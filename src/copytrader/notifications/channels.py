"""Notification channels (Telegram, Discord) and the delivery service.

Guarantees:
* every outgoing text passes through the secret redactor;
* a slow or failing channel never blocks trading (bounded queue, background worker);
* per-channel rate limiting + retries with backoff;
* identical alerts within ``dedupe_window_seconds`` are sent once.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

from copytrader.config.models import AppConfig
from copytrader.core.types import Severity
from copytrader.observability import metrics
from copytrader.resilience.rate_limiter import TokenBucket
from copytrader.security.redaction import REDACTOR

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    body: str
    severity: Severity
    dedupe_key: str | None = None


class Channel(Protocol):
    name: str

    async def send(self, n: Notification) -> None: ...


class TelegramChannel:
    name = "telegram"
    MAX = 4000

    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat = chat_id
        self._client = client or httpx.AsyncClient(timeout=10)
        self.bucket = TokenBucket(rate=1.0, capacity=3)

    async def send(self, n: Notification) -> None:
        text = f"<b>{html.escape(n.title)}</b>\n\n{html.escape(n.body)}"[: self.MAX]
        await self.bucket.acquire()
        resp = await self._client.post(
            self._url,
            json={"chat_id": self._chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        )
        if resp.status_code == 429:
            retry = float((resp.json().get("parameters") or {}).get("retry_after", 5))
            self.bucket.penalize(retry)
            raise RuntimeError(f"telegram rate limited ({retry}s)")
        if resp.status_code >= 400:
            raise RuntimeError(f"telegram HTTP {resp.status_code}")


class DiscordChannel:
    name = "discord"
    MAX = 1900

    def __init__(self, webhook_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = webhook_url
        self._client = client or httpx.AsyncClient(timeout=10)
        self.bucket = TokenBucket(rate=0.4, capacity=3)

    async def send(self, n: Notification) -> None:
        content = f"**{n.title}**\n```\n{n.body}\n```"[: self.MAX]
        await self.bucket.acquire()
        resp = await self._client.post(self._url, json={"content": content, "allowed_mentions": {"parse": []}})
        if resp.status_code == 429:
            self.bucket.penalize(float(resp.json().get("retry_after", 5)))
            raise RuntimeError("discord rate limited")
        if resp.status_code >= 400:
            raise RuntimeError(f"discord HTTP {resp.status_code}")


class NotificationService:
    def __init__(self, channels: list[Channel], config: Callable[[], AppConfig]) -> None:
        self.channels = channels
        self._config = config
        self._queue: asyncio.Queue[Notification] = asyncio.Queue(maxsize=500)
        self._recent: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None
        self._minute: list[float] = []

    @property
    def enabled(self) -> bool:
        return bool(self.channels)

    def submit(self, n: Notification) -> bool:
        cfg = self._config().notifications
        if not self.channels or n.severity.rank < cfg.min_severity.rank:
            return False
        now = time.monotonic()
        if n.dedupe_key:
            last = self._recent.get(n.dedupe_key)
            if last is not None and now - last < cfg.dedupe_window_seconds:
                return False
            self._recent[n.dedupe_key] = now
            if len(self._recent) > 5000:
                cutoff = now - cfg.dedupe_window_seconds
                self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}
        self._minute = [t for t in self._minute if now - t < 60]
        if len(self._minute) >= cfg.max_per_minute and n.severity is not Severity.CRITICAL:
            metrics.NOTIFICATIONS.labels(channel="all", outcome="throttled").inc()
            return False
        self._minute.append(now)
        safe = Notification(REDACTOR.text(n.title), REDACTOR.text(n.body), n.severity, n.dedupe_key)
        try:
            self._queue.put_nowait(safe)
        except asyncio.QueueFull:
            metrics.NOTIFICATIONS.labels(channel="all", outcome="dropped").inc()
            return False
        return True

    def start(self) -> None:
        if self.channels and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._worker())

    async def stop(self, flush_timeout: float = 5.0) -> None:
        if self._task is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=flush_timeout)
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _worker(self) -> None:
        while True:
            n = await self._queue.get()
            try:
                for ch in self.channels:
                    for attempt in range(3):
                        try:
                            await ch.send(n)
                            metrics.NOTIFICATIONS.labels(channel=ch.name, outcome="sent").inc()
                            break
                        except Exception as exc:
                            metrics.NOTIFICATIONS.labels(channel=ch.name, outcome="error").inc()
                            log.warning(
                                "notification_failed",
                                channel=ch.name,
                                attempt=attempt + 1,
                                error=REDACTOR.text(str(exc)),
                            )
                            await asyncio.sleep(2**attempt)
            finally:
                self._queue.task_done()
