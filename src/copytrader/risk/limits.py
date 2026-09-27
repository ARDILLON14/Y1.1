"""Effective risk limits = config, tightened by the operating level and by the
absolute ceilings compiled into the program."""

from __future__ import annotations

from dataclasses import dataclass

from copytrader.config import hard_limits as HL
from copytrader.config.models import AppConfig
from copytrader.core.types import OperatingLevel


@dataclass(frozen=True, slots=True)
class EffectiveLimits:
    capital_usd: float
    max_risk_per_trade_pct: float
    max_risk_per_wallet_pct: float
    max_total_exposure_pct: float
    max_daily_loss_pct: float
    max_weekly_loss_pct: float
    max_monthly_loss_pct: float
    max_open_positions: int
    max_trade_usd: float
    min_trade_usd: float
    max_token_exposure_pct: float
    max_high_risk_exposure_pct: float
    max_slippage_pct: float
    hard_max_trade_usd: float
    level: OperatingLevel

    @classmethod
    def from_config(cls, cfg: AppConfig, level: OperatingLevel) -> EffectiveLimits:
        r = cfg.risk
        values = {
            "max_risk_per_trade_pct": r.max_risk_per_trade_pct,
            "max_total_exposure_pct": min(r.max_total_exposure_pct, HL.HARD_MAX_TOTAL_EXPOSURE_PCT),
            "max_daily_loss_pct": min(r.max_daily_loss_pct, HL.HARD_MAX_DAILY_LOSS_PCT),
            "max_open_positions": min(r.max_open_positions, HL.HARD_MAX_OPEN_POSITIONS),
            "max_trade_usd": r.max_trade_usd,
        }
        if level is OperatingLevel.LIVE_SMALL:
            caps = cfg.levels.level4
            values["max_risk_per_trade_pct"] = min(values["max_risk_per_trade_pct"], caps.max_risk_per_trade_pct)
            values["max_total_exposure_pct"] = min(values["max_total_exposure_pct"], caps.max_total_exposure_pct)
            values["max_daily_loss_pct"] = min(values["max_daily_loss_pct"], caps.max_daily_loss_pct)
            values["max_open_positions"] = min(
                values["max_open_positions"], caps.max_open_positions, HL.HARD_LEVEL4_MAX_OPEN_POSITIONS
            )
            values["max_trade_usd"] = min(values["max_trade_usd"], caps.max_trade_usd, HL.HARD_LEVEL4_MAX_TRADE_USD)
        hard_trade = min(HL.HARD_MAX_TRADE_USD, r.capital_usd * HL.HARD_MAX_TRADE_FRACTION)
        if level is OperatingLevel.LIVE_SMALL:
            hard_trade = min(hard_trade, HL.HARD_LEVEL4_MAX_TRADE_USD)
        return cls(
            capital_usd=r.capital_usd,
            max_risk_per_trade_pct=float(values["max_risk_per_trade_pct"]),
            max_risk_per_wallet_pct=r.max_risk_per_wallet_pct,
            max_total_exposure_pct=float(values["max_total_exposure_pct"]),
            max_daily_loss_pct=float(values["max_daily_loss_pct"]),
            max_weekly_loss_pct=r.max_weekly_loss_pct,
            max_monthly_loss_pct=r.max_monthly_loss_pct,
            max_open_positions=int(values["max_open_positions"]),
            max_trade_usd=min(float(values["max_trade_usd"]), hard_trade),
            min_trade_usd=r.min_trade_usd,
            max_token_exposure_pct=r.max_token_exposure_pct,
            max_high_risk_exposure_pct=r.max_high_risk_exposure_pct,
            max_slippage_pct=min(r.max_slippage_pct, HL.HARD_MAX_ENTRY_SLIPPAGE_PCT),
            hard_max_trade_usd=hard_trade,
            level=level,
        )
