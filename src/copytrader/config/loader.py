"""Load the tunable configuration: defaults < YAML file < env overrides.

Runtime overrides made from the dashboard are layered on top by
``ConfigService`` (see ``service.py``).

Environment overrides use ``COPYTRADER__SECTION__KEY=value`` (double
underscore as separator); values are parsed as YAML so numbers, booleans and
lists work: ``COPYTRADER__SELECTION__TOP_N=20``.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from copytrader.config.models import AppConfig
from copytrader.core.errors import ConfigError

ENV_PREFIX = "COPYTRADER__"
DEFAULT_CONFIG_PATH = "config/settings.yaml"


def deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``patch`` into a copy of ``base`` (dicts merge, rest replaces)."""
    out: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    result: dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = [p.lower() for p in key[len(ENV_PREFIX) :].split("__") if p]
        if not path:
            continue
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:
            value = raw
        node = result
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return result


def read_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{p} must contain a mapping at the top level")
    return data


def load_base_config_dict(path: str | Path | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    cfg_path = path or environ.get("COPYTRADER_CONFIG", DEFAULT_CONFIG_PATH)
    return deep_merge(read_yaml(cfg_path), env_overrides(environ))


def build_config(raw: Mapping[str, Any]) -> AppConfig:
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc)) from exc


def format_validation_error(exc: ValidationError) -> str:
    lines = ["configuración inválida:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(raíz)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
