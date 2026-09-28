"""Measurement endpoints: signal outcomes and attribution of results."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request

from copytrader.api.auth import Session
from copytrader.api.deps import ctx, session
from copytrader.measurement.attribution import AttributionService

router = APIRouter(tags=["analytics"])


@router.get("/analytics")
async def analytics(
    request: Request,
    mode: Literal["paper", "live"] = "paper",
    days: int = Query(30, ge=1, le=365),
    _: Session = Depends(session),
) -> dict[str, Any]:
    c = ctx(request).container
    since = c.clock.now() - timedelta(days=days)
    report = await AttributionService(c.db).report(
        mode=mode, since=since, horizons=c.cfg.measurement.outcome_horizons_minutes
    )
    report["days"] = days
    return report
