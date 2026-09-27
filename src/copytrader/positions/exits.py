"""Exit rules (pure functions, no I/O).

Modes:
* MIRROR     — follow the source wallet's exits; only the emergency stop applies.
* PROTECTED  — follow the source's exits AND apply our own SL / TP / trailing / time.
* SMART      — the source is only an entry signal; our rules manage the exit
               (a source sell only closes us if ``close_on_source_sell`` is on).

The emergency stop applies in every mode: no position may lose more than
``emergency_stop_loss_pct`` without being closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from copytrader.config.models import ExitsSection
from copytrader.core.types import ExitMode


@dataclass(frozen=True, slots=True)
class PositionView:
    entry_price_usd: float
    peak_price_usd: float
    opened_at: datetime
    exit_mode: ExitMode
    tp_levels_hit: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ExitDecision:
    fraction: float  # of the REMAINING quantity
    trigger: str
    reason: str
    tp_level: int | None = None

    @property
    def full(self) -> bool:
        return self.fraction >= 0.999


def evaluate_exit(pos: PositionView, price: float, now: datetime, cfg: ExitsSection) -> ExitDecision | None:
    if pos.entry_price_usd <= 0 or price <= 0:
        return None
    change = price / pos.entry_price_usd - 1
    if change <= -cfg.emergency_stop_loss_pct / 100:
        return ExitDecision(1.0, "emergency_stop",
                            f"Stop de emergencia: {change * 100:.1f}% ≤ -{cfg.emergency_stop_loss_pct:.0f}%")
    if pos.exit_mode is ExitMode.MIRROR:
        return None

    if change <= -cfg.stop_loss_pct / 100:
        return ExitDecision(1.0, "stop_loss", f"Stop loss: {change * 100:.1f}% ≤ -{cfg.stop_loss_pct:.0f}%")

    if cfg.trailing_stop_pct is not None:
        peak = max(pos.peak_price_usd, price)
        peak_gain = peak / pos.entry_price_usd - 1
        if peak_gain >= cfg.trailing_activation_pct / 100:
            drop = 1 - price / peak
            if drop >= cfg.trailing_stop_pct / 100:
                return ExitDecision(1.0, "trailing_stop",
                                    f"Trailing stop: -{drop * 100:.1f}% desde máximo (+{peak_gain * 100:.0f}%)")

    for idx, level in enumerate(cfg.take_profit_levels):
        if idx in pos.tp_levels_hit:
            continue
        if change >= level.gain_pct / 100:
            return ExitDecision(level.sell_fraction, f"take_profit_{idx + 1}",
                                f"Take profit {idx + 1}: +{change * 100:.1f}% ≥ +{level.gain_pct:.0f}% "
                                f"(vende {level.sell_fraction * 100:.0f}%)", tp_level=idx)
        break  # levels are ordered: do not skip ahead

    if cfg.max_hold_minutes is not None:
        age_min = (now - pos.opened_at).total_seconds() / 60
        if age_min >= cfg.max_hold_minutes:
            return ExitDecision(1.0, "max_hold", f"Tiempo máximo de permanencia ({cfg.max_hold_minutes:.0f} min)")
    return None


def source_sell_fraction(sold_fraction: float | None, exit_mode: ExitMode, cfg: ExitsSection) -> float | None:
    """How much of our position to sell when the source wallet sells.

    * ``close_on_source_sell`` → close everything on any source sell (all modes);
    * MIRROR / PROTECTED → sell the same fraction the source sold (all of it
      once the source sold ≥ ``mirror_full_exit_threshold``);
    * SMART → ignore the source's exit (``None``).
    """
    if cfg.close_on_source_sell:
        return 1.0
    if exit_mode is ExitMode.SMART:
        return None
    if sold_fraction is None or sold_fraction >= cfg.mirror_full_exit_threshold:
        return 1.0
    return max(0.0, min(1.0, sold_fraction))
