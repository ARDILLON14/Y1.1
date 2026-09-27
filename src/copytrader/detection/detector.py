"""Runs every suspicious-behaviour rule and applies configured severity overrides."""

from __future__ import annotations

from dataclasses import replace

import structlog

from copytrader.core.models import Flag
from copytrader.detection.rules import ALL_RULES, DetectionContext, Rule

log = structlog.get_logger(__name__)


class SuspicionDetector:
    def __init__(self, rules: tuple[Rule, ...] = ALL_RULES) -> None:
        self.rules = rules

    def detect(self, ctx: DetectionContext) -> list[Flag]:
        flags: list[Flag] = []
        for rule in self.rules:
            try:
                flag = rule(ctx)
            except Exception:  # a buggy rule must not stop the analysis of the wallet
                log.exception("detection_rule_failed", rule=rule.__name__, wallet=ctx.analysis.address)
                continue
            if flag is None:
                continue
            override = ctx.cfg.severity_overrides.get(flag.code)
            flags.append(replace(flag, severity=override) if override else flag)
        return flags
