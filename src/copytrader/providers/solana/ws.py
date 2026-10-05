"""Solana WebSocket streams with automatic reconnection.

Two implementations share the reconnect machinery:

* ``LogsSubscribeStream`` — standard ``logsSubscribe`` with ``mentions`` (one
  subscription per wallet; works with any RPC provider). Notifications carry
  only the signature, the transaction is fetched afterwards.
* ``HeliusTransactionStream`` — Helius ``transactionSubscribe`` with
  ``accountInclude`` (one subscription for all wallets). Notifications carry
  the full transaction: one network round-trip less.

Both emit ``StreamNotice`` objects; ``SolanaSwapFeed`` turns them into swaps.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from copytrader.core.clock import utcnow
from copytrader.observability import metrics
from copytrader.providers.solana.constants import MAX_SUPPORTED_TX_VERSION

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class StreamNotice:
    signature: str
    slot: int
    received_at: datetime
    wallet: str | None = None
    transaction: dict[str, Any] | None = None
    stream: str = ""  # name of the stream that delivered it


NoticeSink = Callable[[StreamNotice], Awaitable[None]]
StatusListener = Callable[[str, bool, str], None]


@dataclass
class StreamSettings:
    url: str
    commitment: str = "confirmed"
    ping_interval: float = 20.0
    stale_timeout: float = 180.0
    max_backoff: float = 60.0
    max_subscriptions_per_connection: int = 100


@dataclass
class _Connection:
    index: int
    wallets: set[str] = field(default_factory=set)
    commands: asyncio.Queue[tuple[str, str]] = field(default_factory=asyncio.Queue)


class ReconnectingStream:
    """Reconnect loop + status reporting. Subclasses implement the protocol."""

    name = "ws"

    def __init__(
        self,
        settings: StreamSettings,
        *,
        name: str | None = None,
        on_status: StatusListener | None = None,
        on_reconnect: Callable[[], Awaitable[None]] | None = None,
        connect: Callable[..., Any] = websockets.connect,
    ) -> None:
        if name:
            self.name = name  # distinguishes a backup stream of the same type
        self.settings = settings
        self._on_status = on_status
        self._on_reconnect = on_reconnect
        self._connect = connect
        self._stopped = asyncio.Event()
        self._ids = itertools.count(1)
        self._bg: set[asyncio.Future[None]] = set()
        self.connected = False

    async def stop(self) -> None:
        self._stopped.set()

    def _status(self, connected: bool, detail: str = "") -> None:
        if connected != self.connected:
            self.connected = connected
            metrics.WS_CONNECTED.labels(stream=self.name).set(1 if connected else 0)
            if self._on_status:
                self._on_status(self.name, connected, detail)

    async def _connection_loop(self, conn: _Connection, sink: NoticeSink) -> None:
        attempt = 0
        first = True
        while not self._stopped.is_set():
            try:
                async with self._connect(
                    self.settings.url,
                    ping_interval=self.settings.ping_interval,
                    ping_timeout=self.settings.ping_interval,
                    max_size=16 * 1024 * 1024,
                    open_timeout=15,
                    close_timeout=5,
                ) as ws:
                    await self._on_open(ws, conn)
                    self._status(True)
                    attempt = 0
                    if not first:
                        metrics.WS_RECONNECTS.labels(stream=self.name).inc()
                        if self._on_reconnect:
                            task = asyncio.ensure_future(self._on_reconnect())
                            self._bg.add(task)
                            task.add_done_callback(self._bg.discard)
                    first = False
                    await self._pump(ws, conn, sink)
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, TimeoutError, InvalidHandshake, InvalidURI, json.JSONDecodeError) as exc:
                self._status(False, f"{type(exc).__name__}: {exc}"[:200])
                log.warning("ws_disconnected", stream=self.name, conn=conn.index, error=type(exc).__name__)
            except Exception as exc:  # unexpected: log and keep reconnecting
                self._status(False, f"{type(exc).__name__}")
                log.exception("ws_error", stream=self.name, conn=conn.index)
            if self._stopped.is_set():
                break
            delay = min(self.settings.max_backoff, 0.5 * (2 ** min(attempt, 10)))
            attempt += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=delay * random.uniform(0.7, 1.3))
        self._status(False, "stopped")

    async def _pump(self, ws: Any, conn: _Connection, sink: NoticeSink) -> None:
        recv_task: asyncio.Task[Any] | None = None
        cmd_task: asyncio.Task[Any] | None = None
        stop_task = asyncio.ensure_future(self._stopped.wait())
        try:
            while not self._stopped.is_set():
                recv_task = recv_task or asyncio.ensure_future(ws.recv())
                cmd_task = cmd_task or asyncio.ensure_future(conn.commands.get())
                done, _ = await asyncio.wait(
                    {recv_task, cmd_task, stop_task},
                    timeout=self.settings.stale_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise TimeoutError("stream stale: no messages")
                if stop_task in done:
                    return
                if cmd_task in done:
                    action, wallet = cmd_task.result()
                    cmd_task = None
                    await self._on_command(ws, conn, action, wallet)
                if recv_task in done:
                    raw = recv_task.result()
                    recv_task = None
                    await self._on_message(json.loads(raw), conn, sink)
        finally:
            for task in (recv_task, cmd_task, stop_task):
                if task is not None and not task.done():
                    task.cancel()

    async def _on_open(self, ws: Any, conn: _Connection) -> None:
        raise NotImplementedError

    async def _on_message(self, msg: dict[str, Any], conn: _Connection, sink: NoticeSink) -> None:
        raise NotImplementedError

    async def _on_command(self, ws: Any, conn: _Connection, action: str, wallet: str) -> None:
        raise NotImplementedError


class LogsSubscribeStream(ReconnectingStream):
    name = "logs_subscribe"

    def __init__(self, settings: StreamSettings, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self._conns: list[_Connection] = []
        self._pending: dict[int, tuple[str, str]] = {}  # request id -> (action, wallet)
        self._subs: dict[int, dict[str, int]] = {}  # conn -> wallet -> subscription id
        self._by_sub: dict[int, dict[int, str]] = {}  # conn -> subscription id -> wallet
        self._tasks: list[asyncio.Task[None]] = []
        self._sink: NoticeSink | None = None

    def set_wallets(self, wallets: set[str]) -> None:
        current = {w for c in self._conns for w in c.wallets}
        for conn in self._conns:
            for wallet in list(conn.wallets - wallets):
                conn.wallets.discard(wallet)
                conn.commands.put_nowait(("unsub", wallet))
        for wallet in sorted(wallets - current):
            free = next(
                (c for c in self._conns if len(c.wallets) < self.settings.max_subscriptions_per_connection), None
            )
            if free is None:
                free = _Connection(index=len(self._conns))
                self._conns.append(free)
                if self._sink is not None:
                    self._tasks.append(asyncio.get_running_loop().create_task(self._connection_loop(free, self._sink)))
            free.wallets.add(wallet)
            free.commands.put_nowait(("sub", wallet))

    async def run(self, sink: NoticeSink) -> None:
        self._sink = sink
        self._tasks = [asyncio.get_running_loop().create_task(self._connection_loop(c, sink)) for c in self._conns]
        await self._stopped.wait()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _send_sub(self, ws: Any, conn: _Connection, wallet: str) -> None:
        req = next(self._ids)
        self._pending[req] = ("sub", wallet)
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req,
                    "method": "logsSubscribe",
                    "params": [{"mentions": [wallet]}, {"commitment": self.settings.commitment}],
                }
            )
        )

    async def _on_open(self, ws: Any, conn: _Connection) -> None:
        self._subs[conn.index] = {}
        self._by_sub[conn.index] = {}
        # Drain queued commands: a fresh connection subscribes to the full set anyway.
        while not conn.commands.empty():
            conn.commands.get_nowait()
        for wallet in sorted(conn.wallets):
            await self._send_sub(ws, conn, wallet)

    async def _on_command(self, ws: Any, conn: _Connection, action: str, wallet: str) -> None:
        if action == "sub" and wallet not in self._subs.get(conn.index, {}):
            await self._send_sub(ws, conn, wallet)
        elif action == "unsub":
            sub_id = self._subs.get(conn.index, {}).pop(wallet, None)
            if sub_id is not None:
                self._by_sub.get(conn.index, {}).pop(sub_id, None)
                await ws.send(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": next(self._ids), "method": "logsUnsubscribe", "params": [sub_id]}
                    )
                )

    async def _on_message(self, msg: dict[str, Any], conn: _Connection, sink: NoticeSink) -> None:
        if "id" in msg and msg.get("id") in self._pending:
            _, wallet = self._pending.pop(msg["id"])
            if "result" in msg and isinstance(msg["result"], int):
                if wallet in conn.wallets:
                    self._subs[conn.index][wallet] = msg["result"]
                    self._by_sub[conn.index][msg["result"]] = wallet
            else:
                log.error("ws_subscribe_failed", wallet=wallet, error=msg.get("error"))
            return
        if msg.get("method") != "logsNotification":
            return
        params = msg.get("params") or {}
        sub_id = params.get("subscription")
        notified = self._by_sub.get(conn.index, {}).get(sub_id) if isinstance(sub_id, int) else None
        result = params.get("result") or {}
        value = result.get("value") or {}
        if value.get("err") is not None or not value.get("signature"):
            return
        await sink(
            StreamNotice(
                signature=value["signature"],
                slot=int((result.get("context") or {}).get("slot") or 0),
                received_at=utcnow(),
                wallet=notified,
                stream=self.name,
            )
        )


class HeliusTransactionStream(ReconnectingStream):
    name = "helius_transaction_subscribe"

    def __init__(self, settings: StreamSettings, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self._conn = _Connection(index=0)
        self._sub_id: int | None = None
        self._pending: dict[int, str] = {}

    def set_wallets(self, wallets: set[str]) -> None:
        if wallets != self._conn.wallets:
            self._conn.wallets = set(wallets)
            self._conn.commands.put_nowait(("resub", ""))

    async def run(self, sink: NoticeSink) -> None:
        task = asyncio.get_running_loop().create_task(self._connection_loop(self._conn, sink))
        await self._stopped.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _subscribe(self, ws: Any) -> None:
        if not self._conn.wallets:
            return
        req = next(self._ids)
        self._pending[req] = "sub"
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": req,
                    "method": "transactionSubscribe",
                    "params": [
                        {"accountInclude": sorted(self._conn.wallets), "failed": False, "vote": False},
                        {
                            "commitment": self.settings.commitment,
                            "encoding": "jsonParsed",
                            "transactionDetails": "full",
                            "showRewards": False,
                            "maxSupportedTransactionVersion": MAX_SUPPORTED_TX_VERSION,
                        },
                    ],
                }
            )
        )

    async def _on_open(self, ws: Any, conn: _Connection) -> None:
        self._sub_id = None
        while not conn.commands.empty():
            conn.commands.get_nowait()
        await self._subscribe(ws)

    async def _on_command(self, ws: Any, conn: _Connection, action: str, wallet: str) -> None:
        old = self._sub_id
        await self._subscribe(ws)  # subscribe new set first, then drop the old one (no gap)
        if old is not None:
            await ws.send(
                json.dumps(
                    {"jsonrpc": "2.0", "id": next(self._ids), "method": "transactionUnsubscribe", "params": [old]}
                )
            )

    async def _on_message(self, msg: dict[str, Any], conn: _Connection, sink: NoticeSink) -> None:
        if "id" in msg and msg.get("id") in self._pending:
            self._pending.pop(msg["id"])
            if isinstance(msg.get("result"), int):
                self._sub_id = msg["result"]
            else:
                log.error("ws_subscribe_failed", stream=self.name, error=msg.get("error"))
            return
        if msg.get("method") != "transactionNotification":
            return
        result = (msg.get("params") or {}).get("result") or {}
        signature = result.get("signature")
        if not signature:
            return
        await sink(
            StreamNotice(
                signature=signature,
                slot=int(result.get("slot") or 0),
                received_at=utcnow(),
                transaction=result,
                stream=self.name,
            )
        )
