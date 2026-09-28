"""Application lifecycle: start services in a safe order, stop them gracefully."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Coroutine
from typing import Any

import structlog

from copytrader.container import Container
from copytrader.core.events import ProviderStatusChanged, SystemMessage
from copytrader.core.types import LEVEL_NAMES_ES, ListType, Severity
from copytrader.db.instance_lock import InstanceLock
from copytrader.db.repositories import EventLogRepo, WalletRepo
from copytrader.observability import metrics

log = structlog.get_logger(__name__)
_metrics_started = False


class Application:
    def __init__(self, container: Container, *, serve_api: bool = True) -> None:
        self.c = container
        self.serve_api = serve_api
        self._tasks: list[asyncio.Task[Any]] = []
        self._stopped = asyncio.Event()
        self._eval_now = asyncio.Event()
        self._api_server: Any = None
        self._lock = InstanceLock(container.db)

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        task.add_done_callback(self._task_done)
        self._tasks.append(task)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled() or self._stopped.is_set():
            return
        exc = task.exception()
        if exc is not None:
            log.error("background_task_crashed", task=task.get_name(), error=repr(exc))
            metrics.ERRORS.labels(component=task.get_name()).inc()

    def trigger_evaluation(self) -> None:
        self._eval_now.set()

    async def start(self) -> None:
        global _metrics_started
        c = self.c
        cfg = c.cfg
        await self._lock.acquire()  # refuses to start if another instance uses this database
        if c.db.is_sqlite:
            await c.db.create_all()
        await c.config_service.load()
        cfg = c.cfg
        await c.mode.load()
        await c.kill.load()
        if cfg.observability.metrics_enabled and not _metrics_started:
            try:
                metrics.start_metrics_server(cfg.observability.metrics_host, cfg.observability.metrics_port)
                _metrics_started = True
            except OSError as exc:
                log.warning("metrics_server_failed", error=str(exc))
        await self._seed_simulated_wallets()
        await c.refresh_tracking()
        recovery = await c.recovery.on_startup()
        c.signals.start()
        requeued = await c.signals.recover()
        c.notifier.start()

        self._spawn(c.providers.feed.run(c.signals.on_swap), "feed")
        self._spawn(self._evaluation_loop(), "evaluation")
        self._spawn(c.positions.run(), "positions")
        self._spawn(c.risk_monitor.run(), "risk_monitor")
        self._spawn(c.recovery.run(), "recovery")
        if c.token_accounts is not None:
            self._spawn(c.token_accounts.run(), "token_accounts")
        self._spawn(self._health_loop(), "health")
        self._spawn(self._housekeeping_loop(), "housekeeping")
        if self.serve_api and cfg.api.enabled:
            self._spawn(self._serve_api(), "api")
        status = c.mode.status()
        c.bus.publish(
            SystemMessage(
                title="Sistema iniciado",
                body=f"Nivel {int(status.level)} ({LEVEL_NAMES_ES[int(status.level)]}), modo "
                f"{status.trade_mode.value if status.trade_mode else 'sin ejecución'}, proveedores "
                f"{cfg.providers.mode}. Recuperación: {recovery} {requeued}",
                severity=Severity.INFO,
            )
        )
        log.info("application_started", level=int(status.level), providers=cfg.providers.mode)

    async def _seed_simulated_wallets(self) -> None:
        market = self.c.providers.simulated_market
        if market is None:
            return
        async with self.c.db.session() as s:
            if await WalletRepo(s).count_tracked() > 0:
                return
        limit = self.c.cfg.wallets.max_wallets
        for w in list(market.wallets.values())[:limit]:
            await self.c.collector.add_wallet(w.address, label=w.label, list_type=ListType.NONE)
        log.info("simulated_wallets_seeded", n=min(limit, len(market.wallets)))

    async def _evaluation_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.c.collector.backfill_all()
                await self.c.cycle.run()
            except Exception:
                metrics.ERRORS.labels(component="evaluation").inc()
                log.exception("evaluation_failed")
            self._eval_now.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._eval_now.wait(), timeout=self.c.cfg.analysis.recompute_interval_seconds)

    async def _health_loop(self) -> None:
        c = self.c
        previous: dict[str, bool] = {}
        while not self._stopped.is_set():
            probes: dict[str, tuple[str, bool, str]] = {}
            try:
                await c.db.ping()
                probes["database"] = ("database", True, "")
            except Exception as exc:
                probes["database"] = ("database", False, type(exc).__name__)
            if c.providers.rpc is not None:
                ok = await c.providers.rpc.get_health()
                probes["solana_rpc_health"] = ("rpc", ok, "" if ok else "getHealth != ok")
            if c.providers.signer is not None and c.providers.live_executor is not None:
                ok = await c.providers.signer.healthy()
                probes["signer"] = ("signer", ok, "" if ok else "firmador inaccesible")
            for name, (kind, ok, detail) in probes.items():
                if ok:
                    c.health.ok(name, kind)
                else:
                    c.health.fail(name, kind, detail)
                if previous.get(name, True) != ok:
                    c.bus.publish(ProviderStatusChanged(provider=name, kind=kind, healthy=ok, detail=detail))
                previous[name] = ok
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=30)

    async def _housekeeping_loop(self) -> None:
        from datetime import timedelta

        while not self._stopped.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=3600)
            try:
                cutoff = self.c.clock.now() - timedelta(days=self.c.cfg.observability.event_log_retention_days)
                async with self.c.db.session() as s:
                    await EventLogRepo(s).purge_before(cutoff)
            except Exception:
                log.exception("housekeeping_failed")

    async def _serve_api(self) -> None:
        import uvicorn

        from copytrader.api.server import create_app

        cfg = self.c.cfg.api
        app = create_app(self.c, self)
        config = uvicorn.Config(
            app,
            host=cfg.host,
            port=cfg.port,
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
        )
        self._api_server = uvicorn.Server(config)
        self._api_server.install_signal_handlers = lambda: None
        await self._api_server.serve()

    async def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        c = self.c
        log.info("application_stopping")
        await c.providers.feed.stop()  # no new signals from here on
        try:
            await asyncio.wait_for(c.signals.drain(), timeout=15)
        except TimeoutError:
            log.warning("signal_queue_not_drained")
        await c.positions.stop()
        await c.risk_monitor.stop()
        await c.recovery.stop()
        if c.token_accounts is not None:
            await c.token_accounts.stop()
        if self._api_server is not None:
            self._api_server.should_exit = True
        await c.signals.stop()
        await c.bus.drain(timeout=5)
        await c.notifier.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._lock.release()
        await c.aclose()
        log.info("application_stopped")

    async def run_forever(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await self.stop()
