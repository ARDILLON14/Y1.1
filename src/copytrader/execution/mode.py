"""Operating level (1-5) and live-trading gates.

Three independent gates must all be open before a single real order is sent:

1. ``app.operating_level`` in the YAML is the *ceiling* (edit + restart);
2. ``levels.live_trading_enabled: true`` in the YAML (edit + restart);
3. the operator *arms* live trading from the dashboard (password), after the
   preflight passes. Arming is kept in memory only: after any restart the
   system comes back **disarmed** and trades paper until re-armed.

The runtime level (≤ ceiling) can be lowered/raised from the dashboard and is
persisted. When the level is live but a gate is closed, entries are simulated
(paper) and the dashboard shows why.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.errors import ConfigError
from copytrader.core.events import EventBus, SystemMessage
from copytrader.core.types import LEVEL_NAMES_ES, OperatingLevel, Severity, TradeMode
from copytrader.db.base import Database
from copytrader.db.repositories import AuditRepo, SystemStateRepo

STATE_KEY = "operating_level"


@dataclass(frozen=True, slots=True)
class ModeStatus:
    ceiling: OperatingLevel
    level: OperatingLevel
    trade_mode: TradeMode | None
    armed: bool
    live_allowed: bool
    live_block_reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "ceiling": int(self.ceiling),
            "level": int(self.level),
            "level_name": LEVEL_NAMES_ES[int(self.level)],
            "trade_mode": self.trade_mode.value if self.trade_mode else None,
            "armed": self.armed,
            "live_allowed": self.live_allowed,
            "live_block_reason": self.live_block_reason,
        }


class ModeController:
    def __init__(self, db: Database, config: Callable[[], AppConfig], clock: Clock, bus: EventBus) -> None:
        self.db = db
        self._config = config
        self.clock = clock
        self.bus = bus
        self._runtime_level: OperatingLevel | None = None
        self._armed = False

    async def load(self) -> None:
        async with self.db.session() as s:
            state = await SystemStateRepo(s).get(STATE_KEY)
        if state and "level" in state:
            try:
                self._runtime_level = OperatingLevel(int(state["level"]))
            except ValueError:
                self._runtime_level = None
        self._armed = False  # never survives a restart

    @property
    def ceiling(self) -> OperatingLevel:
        return self._config().app.operating_level

    @property
    def level(self) -> OperatingLevel:
        ceiling = self.ceiling
        if self._runtime_level is None:
            # Live levels start at paper until the operator raises them explicitly.
            return min(ceiling, OperatingLevel.PAPER) if ceiling.is_live else ceiling
        return min(ceiling, self._runtime_level)

    @property
    def armed(self) -> bool:
        return self._armed

    def live_block_reason(self) -> str | None:
        cfg = self._config()
        if not self.level.is_live:
            return f"Nivel {int(self.level)} ({LEVEL_NAMES_ES[int(self.level)]}) no opera con capital real"
        if not cfg.levels.live_trading_enabled:
            return "levels.live_trading_enabled = false en la configuración"
        if cfg.levels.require_arm and not self._armed:
            return "Trading real no armado desde el dashboard"
        return None

    @property
    def live_allowed(self) -> bool:
        return self.live_block_reason() is None

    @property
    def trade_mode(self) -> TradeMode | None:
        """Book used for NEW entries (None = no execution at this level)."""
        if not self.level.executes:
            return None
        return TradeMode.LIVE if self.live_allowed else TradeMode.PAPER

    def status(self) -> ModeStatus:
        return ModeStatus(
            ceiling=self.ceiling,
            level=self.level,
            trade_mode=self.trade_mode,
            armed=self._armed,
            live_allowed=self.live_allowed,
            live_block_reason=self.live_block_reason(),
        )

    async def set_level(self, level: OperatingLevel, *, actor: str, ip: str | None = None) -> ModeStatus:
        if level > self.ceiling:
            raise ConfigError(
                f"el nivel {int(level)} supera el máximo configurado ({int(self.ceiling)}); "
                "súbelo en config/settings.yaml (app.operating_level) y reinicia"
            )
        old = self.level
        self._runtime_level = level
        if not level.is_live:
            self._armed = False
        async with self.db.session() as s:
            await SystemStateRepo(s).set(STATE_KEY, {"level": int(level)})
            await AuditRepo(s).add(actor, "set_level", str(int(level)), {"old": int(old)}, ip)
        self.bus.publish(
            SystemMessage(
                title="Nivel operativo cambiado",
                body=f"{int(old)} → {int(level)} ({LEVEL_NAMES_ES[int(level)]}) por {actor}",
                severity=Severity.WARNING if level.is_live else Severity.INFO,
            )
        )
        return self.status()

    async def arm(self, *, actor: str, ip: str | None = None) -> ModeStatus:
        cfg = self._config()
        if not self.level.is_live:
            raise ConfigError("sube primero el nivel operativo a 4 o 5")
        if not cfg.levels.live_trading_enabled:
            raise ConfigError("levels.live_trading_enabled está desactivado en la configuración")
        self._armed = True
        async with self.db.session() as s:
            await AuditRepo(s).add(actor, "arm_live", None, {"level": int(self.level)}, ip)
        self.bus.publish(
            SystemMessage(
                title="⚠️ Trading REAL armado",
                body=f"Nivel {int(self.level)} armado por {actor}",
                severity=Severity.CRITICAL,
            )
        )
        return self.status()

    async def disarm(self, *, actor: str, reason: str = "manual", ip: str | None = None) -> ModeStatus:
        was = self._armed
        self._armed = False
        if was:
            async with self.db.session() as s:
                await AuditRepo(s).add(actor, "disarm_live", None, {"reason": reason}, ip)
            self.bus.publish(
                SystemMessage(title="Trading real desarmado", body=f"{actor}: {reason}", severity=Severity.WARNING)
            )
        return self.status()
