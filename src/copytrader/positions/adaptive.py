"""Adaptive exit profile of a position (pure functions, no I/O).

A single stop loss, take profit and time limit for every copy ignores two
things we know at entry:

* **How much the token moves.** A 20 % stop is noise for a token that moves
  30 % an hour and far too loose for one that moves 3 %. The stop is set at
  ``volatility_stop_sigmas`` times the token's expected move over the time we
  expect to hold it, within ``volatility_stop_min_pct``..``_max_pct``. The
  trailing stop scales with it. The position is SIZED with this stop, so the
  capital at risk per trade stays ``risk.max_risk_per_trade_pct``: a wider stop
  means a smaller position, not more risk.
* **How the wallet trades.** Holding a scalper's token for a day, or cutting a
  swing trader's after 24 h, copies neither. The time limit becomes
  ``profile_hold_multiple`` times the wallet's median holding time and the
  take-profit levels scale towards the wallet's median winning trade.

The profile is computed once, when the position opens, and stored with it
(``Position.exit_params["adaptive"]``): the position keeps the stop it was
sized for. Everything not in the profile follows the live configuration.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from copytrader.config.models import ExitsSection, TakeProfitLevel

DEFAULT_HORIZON_HOURS = 1.0  # expected holding time when the wallet's is unknown
MIN_HORIZON_HOURS = 0.25
MAX_HORIZON_HOURS = 24.0


@dataclass(frozen=True, slots=True)
class ExitProfile:
    stop_loss_pct: float | None = None
    trailing_stop_pct: float | None = None
    max_hold_minutes: float | None = None
    take_profit_scale: float | None = None
    notes: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            k: round(v, 3)
            for k, v in (
                ("stop_loss_pct", self.stop_loss_pct),
                ("trailing_stop_pct", self.trailing_stop_pct),
                ("max_hold_minutes", self.max_hold_minutes),
                ("take_profit_scale", self.take_profit_scale),
            )
            if v is not None
        }
        if self.notes:
            out["notes"] = list(self.notes)
        return out

    @property
    def empty(self) -> bool:
        return not self.to_dict()


def _minutes(m: float) -> str:
    return f"{m / 60:.1f} h" if m >= 90 else f"{m:.0f} min"


def build_exit_profile(
    cfg: ExitsSection,
    *,
    hourly_volatility: float | None,
    median_hold_minutes: float | None,
    median_win_pct: float | None,
) -> ExitProfile:
    """Exit parameters for a new position (only the ones that differ from the configuration)."""
    stop = trailing = max_hold = tp_scale = None
    notes: list[str] = []
    hours = (
        min(max(median_hold_minutes / 60, MIN_HORIZON_HOURS), MAX_HORIZON_HOURS)
        if median_hold_minutes
        else DEFAULT_HORIZON_HOURS
    )

    if cfg.volatility_stop and hourly_volatility and hourly_volatility > 0:
        move = hourly_volatility * math.sqrt(hours) * 100
        raw = cfg.volatility_stop_sigmas * move
        stop = min(max(raw, cfg.volatility_stop_min_pct), cfg.volatility_stop_max_pct, cfg.emergency_stop_loss_pct)
        notes.append(
            f"Stop {stop:.1f}%: volatilidad {hourly_volatility * 100:.1f}%/h → movimiento esperado "
            f"{move:.1f}% en ~{_minutes(hours * 60)}, ×{cfg.volatility_stop_sigmas:g}"
            + (" (acotado)" if abs(stop - raw) > 0.05 else "")
        )
        if cfg.trailing_stop_pct is not None:
            ratio = stop / cfg.stop_loss_pct
            trailing = min(
                max(cfg.trailing_stop_pct * ratio, cfg.volatility_stop_min_pct / 2), cfg.volatility_stop_max_pct
            )
            notes.append(f"Trailing {trailing:.1f}% (escalado con el stop)")

    if cfg.wallet_exit_profile and median_hold_minutes and cfg.max_hold_minutes is not None:
        max_hold = min(
            max(cfg.profile_hold_multiple * median_hold_minutes, cfg.profile_min_hold_minutes),
            cfg.profile_max_hold_minutes,
        )
        notes.append(
            f"Tiempo máximo {_minutes(max_hold)}: {cfg.profile_hold_multiple:g}× su holding mediano "
            f"de {_minutes(median_hold_minutes)}"
        )

    if cfg.wallet_exit_profile and cfg.profile_take_profit and median_win_pct and cfg.take_profit_levels:
        first = cfg.take_profit_levels[0].gain_pct
        tp_scale = min(max(median_win_pct / first, cfg.profile_tp_min_scale), cfg.profile_tp_max_scale)
        if abs(tp_scale - 1) < 0.01:
            tp_scale = None
        else:
            notes.append(
                f"Take profit ×{tp_scale:.2f}: su ganancia mediana es +{median_win_pct:.0f}% "
                f"(primer nivel +{first * tp_scale:.0f}%)"
            )
    return ExitProfile(stop, trailing, max_hold, tp_scale, tuple(notes))


def effective_exits(cfg: ExitsSection, adaptive: Mapping[str, Any] | None) -> ExitsSection:
    """The live exit configuration with a position's stored profile applied."""
    if not adaptive:
        return cfg
    update: dict[str, Any] = {}
    stop = adaptive.get("stop_loss_pct")
    if isinstance(stop, int | float) and stop > 0:
        update["stop_loss_pct"] = min(float(stop), cfg.emergency_stop_loss_pct)
    trailing = adaptive.get("trailing_stop_pct")
    if isinstance(trailing, int | float) and trailing > 0 and cfg.trailing_stop_pct is not None:
        update["trailing_stop_pct"] = float(trailing)
    hold = adaptive.get("max_hold_minutes")
    if isinstance(hold, int | float) and hold > 0 and cfg.max_hold_minutes is not None:
        update["max_hold_minutes"] = float(hold)
    scale = adaptive.get("take_profit_scale")
    if isinstance(scale, int | float) and scale > 0 and cfg.take_profit_levels:
        update["take_profit_levels"] = [
            TakeProfitLevel(gain_pct=lvl.gain_pct * float(scale), sell_fraction=lvl.sell_fraction)
            for lvl in cfg.take_profit_levels
        ]
    return cfg.model_copy(update=update) if update else cfg
