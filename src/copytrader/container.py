"""Composition root: builds every component from the configuration.

This is the only place that knows concrete classes. Swapping a provider
(e.g. Helius stream instead of logsSubscribe, a future EVM adapter, the
simulated market) is a change here, not in the business logic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from copytrader.alerts.service import AlertService
from copytrader.collector.wallet_collector import WalletCollector
from copytrader.config.models import AppConfig
from copytrader.config.secrets import Secrets
from copytrader.config.service import ConfigService
from copytrader.core.clock import Clock, SystemClock
from copytrader.core.concurrency import KeyedLocks
from copytrader.core.events import EventBus, ProviderStatusChanged
from copytrader.core.models import TokenInfo
from copytrader.core.types import TradeMode
from copytrader.db.base import Database
from copytrader.db.repositories import DbConfigStore, TokenRepo
from copytrader.execution.base import Executor
from copytrader.execution.live import LiveExecutor
from copytrader.execution.mode import ModeController
from copytrader.execution.paper import PaperExecutor
from copytrader.execution.recovery import OrderRecovery
from copytrader.execution.service import ExecutionGuard, ExecutionService
from copytrader.notifications.channels import Channel, DiscordChannel, NotificationService, TelegramChannel
from copytrader.observability import metrics
from copytrader.observability.health import HealthRegistry
from copytrader.pipeline.copy_pipeline import CopyPipeline
from copytrader.positions.manager import PositionManager
from copytrader.providers.interfaces import HistorySource, QuoteSource, SolPriceHistory, SwapFeed
from copytrader.providers.token_info import TokenCategorizer, TokenInfoService
from copytrader.resilience.circuit_breaker import CircuitBreaker
from copytrader.resilience.http import ResilientHttp
from copytrader.resilience.retry import RetryPolicy
from copytrader.risk.engine import RiskEngine
from copytrader.risk.killswitch import KillSwitchService
from copytrader.risk.monitor import RiskMonitor
from copytrader.scoring.cycle import EvaluationCycle
from copytrader.security.redaction import REDACTOR
from copytrader.signals.engine import SignalEngine

log = structlog.get_logger(__name__)


@dataclass
class Providers:
    feed: SwapFeed
    history: HistorySource
    tokens: TokenInfoService
    quotes: QuoteSource
    sol_history: SolPriceHistory
    live_executor: LiveExecutor | None = None
    rpc: Any = None
    simulated_market: Any = None
    http_clients: list[ResilientHttp] = field(default_factory=list)
    signer: Any = None


class Container:
    def __init__(self, config: ConfigService, secrets: Secrets, *, clock: Clock | None = None,
                 db: Database | None = None) -> None:
        self.config_service = config
        self.secrets = secrets
        self.clock = clock or SystemClock()
        self.bus = EventBus()
        self.health = HealthRegistry()
        self.db = db or Database(secrets.database_url.get_secret_value())
        self.token_locks = KeyedLocks()
        REDACTOR.register(secrets.all_secret_values())
        self._token_persist_at: dict[str, float] = {}
        self._bg: set[asyncio.Task[None]] = set()

        cfg = self.cfg
        self.providers = self._build_providers(cfg)
        self.tokens = self.providers.tokens
        self.collector = WalletCollector(self.db, self.providers.history, self.tokens, self.clock, self.get_cfg,
                                         address_validator=self._address_validator())
        self.mode = ModeController(self.db, self.get_cfg, self.clock, self.bus)
        self.kill = KillSwitchService(self.db, self.clock, self.bus)
        self.risk = RiskEngine(db=self.db, config=self.get_cfg, clock=self.clock, bus=self.bus, kill=self.kill,
                               mode=self.mode)
        self.risk_monitor = RiskMonitor(db=self.db, clock=self.clock, config=self.get_cfg, risk=self.risk,
                                        mode=self.mode)
        executors: dict[TradeMode, Executor] = {
            TradeMode.PAPER: PaperExecutor(self.providers.quotes, self.tokens, self.clock, self.get_cfg)}
        if self.providers.live_executor is not None:
            executors[TradeMode.LIVE] = self.providers.live_executor
        self.execution = ExecutionService(db=self.db, clock=self.clock, config=self.get_cfg, bus=self.bus,
                                          executors=executors,
                                          guard=ExecutionGuard(self.get_cfg, self.mode, self.kill, self.risk),
                                          risk=self.risk)
        self.signals = SignalEngine(db=self.db, clock=self.clock, config=self.get_cfg, bus=self.bus, mode=self.mode,
                                    chain=cfg.app.chain)
        self.positions = PositionManager(db=self.db, clock=self.clock, config=self.get_cfg, bus=self.bus,
                                         execution=self.execution, tokens=self.tokens, token_locks=self.token_locks)
        self.execution.fill_applier = self.positions
        self.pipeline = CopyPipeline(db=self.db, clock=self.clock, config=self.get_cfg, bus=self.bus, mode=self.mode,
                                     risk=self.risk, execution=self.execution, tokens=self.tokens,
                                     signals=self.signals, token_locks=self.token_locks)
        self.signals.entry_handler = self.pipeline
        self.signals.exit_handler = self.positions
        self.kill.on_global_flatten = self.positions.close_all
        price_at = None
        if self.providers.simulated_market is not None:
            market = self.providers.simulated_market
            price_at = market.token_price  # the simulator has full price paths
        self.cycle = EvaluationCycle(db=self.db, clock=self.clock, config=self.get_cfg, bus=self.bus,
                                     tokens=self.tokens, sol_history=self.providers.sol_history, price_at=price_at)
        self.cycle.on_selection(self._on_selection)
        self.recovery = OrderRecovery(db=self.db, clock=self.clock, config=self.get_cfg, bus=self.bus,
                                      execution=self.execution, live=self.providers.live_executor,
                                      tokens=self.tokens, kill=self.kill)
        self.notifier = NotificationService(self._channels(cfg), self.get_cfg)
        self.alerts = AlertService(self.db, self.clock, self.get_cfg, self.notifier)
        self.alerts.subscribe(self.bus)

    # ----------------------------------------------------------------- config
    @property
    def cfg(self) -> AppConfig:
        return self.config_service.current

    def get_cfg(self) -> AppConfig:
        return self.config_service.current

    # -------------------------------------------------------------- providers
    def _http(self, name: str, *, timeout: float, rate: float, headers: dict[str, str] | None = None) -> ResilientHttp:
        cfg = self.cfg.providers
        breaker = CircuitBreaker(name, cfg.circuit_breaker.failure_threshold, cfg.circuit_breaker.reset_timeout_seconds,
                                 on_state_change=self._on_breaker)
        client = ResilientHttp(name, timeout=timeout, rate_per_second=rate,
                               retry=RetryPolicy(cfg.retry.max_attempts, cfg.retry.base_delay_seconds,
                                                 cfg.retry.max_delay_seconds),
                               breaker=breaker, health=self.health, headers=headers)
        self.providers_http.append(client)
        return client

    def _on_breaker(self, name: str, old: Any, new: Any) -> None:
        metrics.set_circuit_state(name, new.value)
        healthy = new.value == "closed"
        kind = "rpc" if "rpc" in name else "http"
        self.bus.publish(ProviderStatusChanged(provider=name, kind=kind, healthy=healthy,
                                               detail=f"circuit breaker {old.value} → {new.value}"))

    def _address_validator(self) -> Any:
        if self.cfg.providers.mode == "simulated":
            return lambda a: isinstance(a, str) and 32 <= len(a) <= 44
        from copytrader.providers.solana.constants import is_valid_address

        return is_valid_address

    def _on_token_update(self, info: TokenInfo) -> None:
        now = self.clock.monotonic()
        if now - self._token_persist_at.get(info.mint, -1e9) < 60:
            return
        self._token_persist_at[info.mint] = now

        async def persist() -> None:
            try:
                async with self.db.session() as s:
                    await TokenRepo(s).upsert(info)
            except Exception:
                log.debug("token_persist_failed", mint=info.mint)

        try:
            task = asyncio.get_running_loop().create_task(persist())
        except RuntimeError:
            return
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _build_providers(self, cfg: AppConfig) -> Providers:
        self.providers_http: list[ResilientHttp] = []
        categorizer = TokenCategorizer.from_file(cfg.providers.token_categories_file)
        if cfg.providers.mode == "simulated":
            from copytrader.providers import simulated as sim

            s = cfg.providers.simulated
            market = sim.SimulatedMarket(clock=self.clock, seed=s.seed, n_wallets=s.n_wallets, n_tokens=s.n_tokens,
                                         history_days=s.history_days,
                                         realtime_trades_per_minute=s.realtime_trades_per_minute, speedup=s.speedup)
            tokens = TokenInfoService(market=sim.SimulatedMarketData(market), prices=sim.SimulatedPrices(market),
                                      clock=self.clock, config=self.get_cfg, risk=sim.SimulatedTokenRisk(market),
                                      mint_source=sim.SimulatedMintInfo(market), categorizer=categorizer,
                                      on_update=self._on_token_update)
            return Providers(feed=sim.SimulatedFeed(market), history=sim.SimulatedHistorySource(market),
                             tokens=tokens, quotes=sim.SimulatedQuotes(market),
                             sol_history=sim.SimulatedSolHistory(market), simulated_market=market)
        return self._build_live_providers(cfg, categorizer)

    def _build_live_providers(self, cfg: AppConfig, categorizer: TokenCategorizer) -> Providers:
        from copytrader.providers.dexscreener import DexScreenerClient
        from copytrader.providers.jupiter import JupiterClient
        from copytrader.providers.prices import KlinesSolPriceHistory
        from copytrader.providers.rugcheck import RugCheckClient
        from copytrader.providers.solana.feed import SolanaSwapFeed
        from copytrader.providers.solana.history import RpcHistorySource
        from copytrader.providers.solana.mint import RpcMintInfoSource
        from copytrader.providers.solana.rpc import SolanaRpc
        from copytrader.providers.solana.ws import HeliusTransactionStream, LogsSubscribeStream, StreamSettings

        p = cfg.providers
        sol = p.solana
        rpc_url = self.secrets.rpc_http_url(sol.rpc_http_url)
        ws_url = self.secrets.rpc_ws_url(sol.rpc_ws_url)
        rpc = SolanaRpc(self._http("solana_rpc", timeout=sol.timeout_seconds, rate=sol.rate_limit_per_second),
                        rpc_url, sol.commitment)
        jup_key = self.secrets.jupiter_api_key.get_secret_value() if self.secrets.jupiter_api_key else None
        jupiter = JupiterClient(self._http("jupiter", timeout=p.jupiter.timeout_seconds,
                                           rate=p.jupiter.rate_limit_per_second),
                                quote_url=p.jupiter.quote_url, swap_url=p.jupiter.swap_url,
                                price_url=p.jupiter.price_url, api_key=jup_key,
                                restrict_intermediate_tokens=p.jupiter.restrict_intermediate_tokens, clock=self.clock)
        dex = DexScreenerClient(self._http("dexscreener", timeout=p.dexscreener.timeout_seconds,
                                           rate=p.dexscreener.rate_limit_per_second), p.dexscreener.base_url)
        rug = (RugCheckClient(self._http("rugcheck", timeout=p.rugcheck.timeout_seconds,
                                         rate=p.rugcheck.rate_limit_per_second), p.rugcheck.base_url)
               if p.rugcheck.enabled else None)
        sol_history = KlinesSolPriceHistory(self._http("sol_price_history", timeout=10, rate=2),
                                            p.sol_price_history_url, p.sol_price_symbol)
        tokens = TokenInfoService(market=dex, prices=jupiter, clock=self.clock, config=self.get_cfg, risk=rug,
                                  mint_source=RpcMintInfoSource(rpc), categorizer=categorizer,
                                  on_update=self._on_token_update)
        history = RpcHistorySource(rpc, sol_history, quote_mints=p.quote_mints,
                                   concurrency=sol.backfill_concurrency, clock=self.clock)
        settings = StreamSettings(url=ws_url, commitment=sol.commitment, ping_interval=sol.ws_ping_interval_seconds,
                                  stale_timeout=sol.ws_stale_timeout_seconds,
                                  max_backoff=sol.reconnect_max_backoff_seconds,
                                  max_subscriptions_per_connection=sol.max_subscriptions_per_connection)
        stream_cls = HeliusTransactionStream if sol.stream == "helius_transaction_subscribe" else LogsSubscribeStream
        stream = stream_cls(settings, on_status=self._on_stream_status)
        feed = SolanaSwapFeed(stream, rpc, history, sol_price=tokens.sol_price, cursor_lookup=self._cursor,
                              quote_mints=p.quote_mints, clock=self.clock, tx_retries=sol.get_transaction_retries,
                              tx_retry_delay=sol.get_transaction_retry_delay_ms / 1000,
                              reconcile_interval=sol.reconcile_poll_interval_seconds,
                              dedupe_size=cfg.signals.dedupe_cache_size)
        signer = self._build_signer(cfg)
        live = None
        if signer is not None and cfg.execution.wallet_public_key:
            live = LiveExecutor(quotes=jupiter, builder=jupiter, chain=rpc, signer=signer, tokens=tokens,
                                clock=self.clock, config=self.get_cfg)
        return Providers(feed=feed, history=history, tokens=tokens, quotes=jupiter, sol_history=sol_history,
                         live_executor=live, rpc=rpc, signer=signer)

    async def _cursor(self, address: str) -> str | None:
        return await self.collector.cursor(address)

    def _on_stream_status(self, name: str, connected: bool, detail: str) -> None:
        if connected:
            self.health.ok(name, "websocket")
        else:
            self.health.fail(name, "websocket", detail)
        self.bus.publish(ProviderStatusChanged(provider=name, kind="websocket", healthy=connected, detail=detail))

    def _build_signer(self, cfg: AppConfig) -> Any:
        from copytrader.security.signer import LocalSigner, NullSigner, RemoteSigner
        from copytrader.security.signer_policy import SignerLimits, SignerPolicy

        sec = cfg.security
        if sec.signer_mode == "none":
            return NullSigner()
        owner = cfg.execution.wallet_public_key
        if not owner:
            log.error("signer_configured_without_wallet_public_key")
            return NullSigner()
        policy = SignerPolicy(owner=owner, limits=SignerLimits(
            max_notional_usd_per_tx=cfg.risk.max_trade_usd * 1.05,
            max_notional_usd_per_day=cfg.risk.capital_usd,
            max_priority_fee_lamports=max(cfg.execution.priority_fee_max_lamports, 1),
            max_tip_lamports=max(cfg.execution.jito_tip_lamports, 1)))
        if sec.signer_mode == "remote":
            if not self.secrets.signer_hmac_key:
                raise RuntimeError("SIGNER_HMAC_KEY es obligatorio con security.signer_mode = remote")
            return RemoteSigner(sec.signer_url, self.secrets.signer_hmac_key.get_secret_value().encode(),
                                expected_pubkey=owner, timeout=sec.signer_timeout_seconds, local_policy=policy)
        from copytrader.security.keystore import load_keypair

        if not self.secrets.keystore_passphrase:
            raise RuntimeError("KEYSTORE_PASSPHRASE es obligatorio con security.signer_mode = local")
        keypair = load_keypair(sec.keystore_path, self.secrets.keystore_passphrase.get_secret_value())
        REDACTOR.register([str(keypair)])
        return LocalSigner(keypair, policy)

    def _channels(self, cfg: AppConfig) -> list[Channel]:
        channels: list[Channel] = []
        n = cfg.notifications
        if n.telegram_enabled and self.secrets.telegram_bot_token and n.telegram_chat_id:
            channels.append(TelegramChannel(self.secrets.telegram_bot_token.get_secret_value(), n.telegram_chat_id))
        if n.discord_enabled and self.secrets.discord_webhook_url:
            channels.append(DiscordChannel(self.secrets.discord_webhook_url.get_secret_value()))
        return channels

    # ------------------------------------------------------------- listeners
    async def _on_selection(self, _: Any) -> None:
        await self.refresh_tracking()

    async def refresh_tracking(self) -> None:
        await self.signals.refresh_wallets()
        self.providers.feed.set_wallets(self.signals.tracked_addresses)

    async def aclose(self) -> None:
        for client in self.providers_http:
            await client.aclose()
        await self.db.dispose()


def build_config_service(db: Database, base_raw: dict[str, Any]) -> ConfigService:
    return ConfigService(base_raw, DbConfigStore(db))
