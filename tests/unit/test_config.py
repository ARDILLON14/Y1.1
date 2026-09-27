import pytest

from copytrader.config.loader import build_config, deep_merge, env_overrides
from copytrader.config.models import AppConfig
from copytrader.config.service import ConfigService, ConfigVersion
from copytrader.core.errors import ConfigError


class MemoryStore:
    def __init__(self):
        self.versions: list[ConfigVersion] = []

    async def load_latest(self):
        return self.versions[-1] if self.versions else None

    async def save(self, version):
        self.versions.append(version)

    async def get(self, version):
        return next((v for v in self.versions if v.version == version), None)


def test_defaults_are_valid_and_safe():
    cfg = AppConfig()
    assert cfg.app.operating_level == 1  # analysis only by default
    assert cfg.providers.mode == "simulated"
    assert cfg.levels.live_trading_enabled is False


def test_hard_limit_rejects_oversized_trade():
    with pytest.raises(ConfigError):
        build_config({"risk": {"capital_usd": 1000, "max_trade_usd": 500}})


def test_loss_limits_must_be_ordered():
    with pytest.raises(ConfigError):
        build_config({"risk": {"max_daily_loss_pct": 15, "max_weekly_loss_pct": 10}})


def test_live_level_requires_signer_and_live_providers():
    with pytest.raises(ConfigError):
        build_config({"app": {"operating_level": 5}})


def test_take_profit_levels_must_increase():
    with pytest.raises(ConfigError):
        build_config(
            {
                "exits": {
                    "take_profit_levels": [
                        {"gain_pct": 100, "sell_fraction": 0.5},
                        {"gain_pct": 50, "sell_fraction": 1},
                    ]
                }
            }
        )


def test_unknown_keys_are_rejected():
    with pytest.raises(ConfigError):
        build_config({"risk": {"max_dayly_loss_pct": 3}})


def test_env_overrides_parse_types():
    env = {"COPYTRADER__SELECTION__TOP_N": "20", "COPYTRADER__RISK__ALLOW_ADD_TO_POSITION": "true", "OTHER": "x"}
    assert env_overrides(env) == {"selection": {"top_n": 20}, "risk": {"allow_add_to_position": True}}


def test_deep_merge_does_not_mutate():
    base = {"a": {"b": 1, "c": 2}}
    merged = deep_merge(base, {"a": {"b": 5}})
    assert merged == {"a": {"b": 5, "c": 2}}
    assert base == {"a": {"b": 1, "c": 2}}


async def test_service_applies_versions_and_rolls_back():
    store = MemoryStore()
    svc = ConfigService({}, store)
    await svc.apply_patch({"selection": {"top_n": 20}}, author="me")
    assert svc.current.selection.top_n == 20
    assert svc.version == 1
    await svc.apply_patch({"risk": {"max_open_positions": 4}}, author="me")
    assert svc.current.selection.top_n == 20  # previous override kept
    await svc.rollback(1, author="me")
    assert svc.current.risk.max_open_positions == 10
    assert svc.version == 3


async def test_service_rejects_invalid_patch_and_keeps_config():
    svc = ConfigService({}, MemoryStore())
    with pytest.raises(ConfigError):
        await svc.apply_patch({"risk": {"max_trade_usd": 999999}}, author="me")
    assert svc.current.risk.max_trade_usd == 100


async def test_service_blocks_non_runtime_sections():
    svc = ConfigService({}, MemoryStore())
    with pytest.raises(ConfigError):
        await svc.apply_patch({"app": {"operating_level": 5}}, author="me")
    with pytest.raises(ConfigError):
        await svc.apply_patch({"levels": {"live_trading_enabled": True}}, author="me")
    with pytest.raises(ConfigError):
        await svc.apply_patch({"security": {"signer_mode": "local"}}, author="me")


async def test_service_load_ignores_invalid_stored_overrides():
    store = MemoryStore()
    store.versions.append(ConfigVersion(3, {"risk": {"max_trade_usd": 10**9}}, "x", ""))
    svc = ConfigService({}, store)
    cfg = await svc.load()
    assert cfg.risk.max_trade_usd == 100


def test_example_settings_file_is_valid_and_complete():
    from pathlib import Path

    from copytrader.config.loader import read_yaml

    raw = read_yaml(Path(__file__).resolve().parents[2] / "config" / "settings.example.yaml")
    cfg = build_config(raw)
    assert set(raw) == set(AppConfig.model_fields)  # every section documented
    assert cfg.app.operating_level == 1 and not cfg.levels.live_trading_enabled
