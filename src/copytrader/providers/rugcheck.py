"""RugCheck token risk summary."""

from __future__ import annotations

from typing import Any

from copytrader.core.errors import ProviderError
from copytrader.providers.interfaces import RiskData
from copytrader.resilience.http import ResilientHttp


class RugCheckClient:
    def __init__(self, http: ResilientHttp, base_url: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def token_risk(self, mint: str) -> RiskData | None:
        try:
            data = await self.http.get_json(f"{self.base_url}/tokens/{mint}/report/summary")
        except ProviderError as exc:
            if exc.status_code in (400, 404):
                return None  # unknown token: caller treats as "risk unknown"
            raise
        return parse_rugcheck(mint, data)


def parse_rugcheck(mint: str, data: Any) -> RiskData:
    if not isinstance(data, dict):
        return RiskData(mint=mint)
    risks: list[dict[str, Any]] = [r for r in data.get("risks") or [] if isinstance(r, dict)]
    names = [str(r.get("name", "")) for r in risks]
    levels = [str(r.get("level", "")).lower() for r in risks]
    normalised = data.get("score_normalised")
    score: float | None
    try:
        score = float(normalised) if normalised is not None else None
    except (TypeError, ValueError):
        score = None
    if score is None:
        raw = data.get("score")
        try:
            # Raw scores are unbounded; ~1000+ is very risky. Map to 0..100.
            score = min(100.0, float(raw) / 10.0) if raw is not None else None
        except (TypeError, ValueError):
            score = None
    if score is not None:
        score = max(0.0, min(100.0, score))
    rugged = bool(data.get("rugged")) or any("rug" in n.lower() for n in names)
    if rugged:
        level = "critical"
    elif "danger" in levels or (score is not None and score >= 60):
        level = "high"
    elif "warn" in levels or (score is not None and score >= 30):
        level = "medium"
    else:
        level = "low"
    flags = [f"{lvl}:{name}" for name, lvl in zip(names, levels, strict=False)]
    return RiskData(mint=mint, score=score, level=level, flags=flags, is_rugged=rugged)
