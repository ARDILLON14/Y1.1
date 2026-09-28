"""Runtime configuration with versioned overrides.

``current`` is always a fully validated ``AppConfig``. Changes are applied as
deep-merge patches on top of the base (YAML + env), validated as a whole
(including cross-section rules and hard limits) and persisted as a new version
before becoming visible. Invalid patches never replace a working config.

Sections that affect wiring (providers, api, security, observability...) are
not runtime-mutable: they require editing the YAML and restarting, which is
deliberate friction for security-relevant settings. The operating level has
its own guarded flow (password + preflight) in ``ModeController``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import structlog

from copytrader.config.loader import build_config, deep_merge
from copytrader.config.models import AppConfig
from copytrader.core.errors import ConfigError

log = structlog.get_logger(__name__)

RUNTIME_MUTABLE_SECTIONS: frozenset[str] = frozenset(
    {
        "wallets",
        "analysis",
        "scoring",
        "status_rules",
        "detection",
        "selection",
        "signals",
        "risk",
        "sizing",
        "latency",
        "exits",
        "execution",
        "paper",
        "levels",
        "notifications",
        "backtest",
        "measurement",
    }
)
# Individual keys inside mutable sections that still require a restart / YAML edit.
LOCKED_KEYS: frozenset[str] = frozenset(
    {
        "levels.live_trading_enabled",
        "levels.require_arm",
        "execution.wallet_public_key",
        "execution.quote_mint",
    }
)


@dataclass(frozen=True, slots=True)
class ConfigVersion:
    version: int
    overrides: dict[str, Any]
    author: str
    comment: str
    created_at: datetime | None = None


class ConfigStore(Protocol):
    async def load_latest(self) -> ConfigVersion | None: ...

    async def save(self, version: ConfigVersion) -> None: ...

    async def get(self, version: int) -> ConfigVersion | None: ...


Listener = Callable[[AppConfig, AppConfig], Awaitable[None] | None]


def _flatten(d: Mapping[str, Any], prefix: str = "") -> list[str]:
    keys: list[str] = []
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, Mapping) and v:
            keys.extend(_flatten(v, path))
        else:
            keys.append(path)
    return keys


def validate_patch_scope(patch: Mapping[str, Any]) -> None:
    for key in _flatten(patch):
        section = key.split(".", 1)[0]
        if section not in RUNTIME_MUTABLE_SECTIONS:
            raise ConfigError(f"'{key}' no se puede cambiar en caliente; edita config/settings.yaml y reinicia")
        if any(key == locked or key.startswith(locked + ".") for locked in LOCKED_KEYS):
            raise ConfigError(f"'{key}' está bloqueada; solo puede cambiarse en el YAML")


class ConfigService:
    def __init__(self, base_raw: Mapping[str, Any], store: ConfigStore | None = None) -> None:
        self._base_raw = dict(base_raw)
        self._store = store
        self._overrides: dict[str, Any] = {}
        self._version = 0
        self._current = build_config(self._base_raw)
        self._listeners: list[Listener] = []
        self._lock = asyncio.Lock()

    @property
    def current(self) -> AppConfig:
        return self._current

    @property
    def version(self) -> int:
        return self._version

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)

    @property
    def base(self) -> AppConfig:
        return build_config(self._base_raw)

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def load(self) -> AppConfig:
        """Apply the latest persisted overrides (called once at startup)."""
        if self._store is None:
            return self._current
        latest = await self._store.load_latest()
        if latest is None:
            return self._current
        try:
            validate_patch_scope(latest.overrides)
            cfg = build_config(deep_merge(self._base_raw, latest.overrides))
        except ConfigError as exc:
            # Never refuse to start because of stale overrides: fall back to YAML.
            log.error("config_overrides_invalid_ignored", version=latest.version, error=str(exc))
            self._version = latest.version
            return self._current
        self._overrides = dict(latest.overrides)
        self._version = latest.version
        self._current = cfg
        return cfg

    def preview(self, patch: Mapping[str, Any]) -> AppConfig:
        validate_patch_scope(patch)
        return build_config(deep_merge(self._base_raw, deep_merge(self._overrides, patch)))

    async def apply_patch(self, patch: Mapping[str, Any], *, author: str, comment: str = "") -> AppConfig:
        async with self._lock:
            new_cfg = self.preview(patch)
            new_overrides = deep_merge(self._overrides, patch)
            await self._commit(new_cfg, new_overrides, author=author, comment=comment)
            return new_cfg

    async def reset_overrides(self, *, author: str, comment: str = "reset") -> AppConfig:
        async with self._lock:
            cfg = build_config(self._base_raw)
            await self._commit(cfg, {}, author=author, comment=comment)
            return cfg

    async def rollback(self, version: int, *, author: str) -> AppConfig:
        if self._store is None:
            raise ConfigError("no config store configured")
        target = await self._store.get(version)
        if target is None:
            raise ConfigError(f"versión {version} no encontrada")
        async with self._lock:
            validate_patch_scope(target.overrides)
            cfg = build_config(deep_merge(self._base_raw, target.overrides))
            await self._commit(cfg, dict(target.overrides), author=author, comment=f"rollback a v{version}")
            return cfg

    async def _commit(self, cfg: AppConfig, overrides: dict[str, Any], *, author: str, comment: str) -> None:
        version = ConfigVersion(self._version + 1, overrides, author, comment)
        if self._store is not None:
            await self._store.save(version)  # persist first: a crash never loses an applied change
        old = self._current
        self._overrides = overrides
        self._version = version.version
        self._current = cfg
        log.info("config_updated", version=version.version, author=author, comment=comment)
        for listener in self._listeners:
            try:
                result = listener(old, cfg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("config_listener_failed")
