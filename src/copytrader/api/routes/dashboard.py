"""Overview, equity, risk, kill switches, system/levels, config, alerts, audit."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from copytrader.api import serializers as ser
from copytrader.api.auth import Session
from copytrader.api.deps import client_ip, ctx, reauth, session, write_session
from copytrader.config.service import RUNTIME_MUTABLE_SECTIONS
from copytrader.core.types import KillSwitchScope, OperatingLevel, TradeMode
from copytrader.db.repositories import (
    AlertRepo,
    AuditRepo,
    DbConfigStore,
    EquityRepo,
    RiskEventRepo,
    SignalRepo,
)
from copytrader.preflight import preflight_passed, run_preflight

router = APIRouter(tags=["dashboard"])


def _book_mode(c: Any) -> TradeMode:
    return c.mode.trade_mode or TradeMode.PAPER


@router.get("/overview")
async def overview(request: Request, _: Session = Depends(session)) -> dict[str, Any]:
    c = ctx(request).container
    mode = _book_mode(c)
    book = await c.risk.book(mode, include_reservations=False)
    lim = c.risk.limits()
    cap = min(lim.capital_usd, book.equity_usd) or lim.capital_usd
    async with c.db.session() as s:
        signal_counts = await SignalRepo(s).counts_since(c.clock.now() - timedelta(hours=24))
    from copytrader.db.repositories import WalletRepo

    async with c.db.session() as s:
        wallets = await WalletRepo(s).list()
    by_status: dict[str, int] = {}
    for w in wallets:
        by_status[w.status] = by_status.get(w.status, 0) + 1
    report = c.cycle.last_report
    return {
        "mode": c.mode.status().to_dict(),
        "book": book.to_dict(),
        "limits": {
            "capital_usd": lim.capital_usd, "max_open_positions": lim.max_open_positions,
            "max_total_exposure_pct": lim.max_total_exposure_pct, "max_daily_loss_pct": lim.max_daily_loss_pct,
            "max_trade_usd": lim.max_trade_usd, "hard_max_trade_usd": lim.hard_max_trade_usd,
        },
        "risk_used": {
            "exposure": book.exposure_usd / (cap * lim.max_total_exposure_pct / 100) if cap else 0.0,
            "positions": len(book.exposures) / lim.max_open_positions,
            "daily_loss": book.loss_pct(book.day_start_equity) / lim.max_daily_loss_pct,
        },
        "kill_switches": c.kill.snapshot(),
        "wallets": {"total": len(wallets), "by_status": by_status, "selected": sum(1 for w in wallets if w.selected)},
        "signals_24h": signal_counts,
        "health": {"overall": c.health.overall().value, "components": c.health.snapshot()},
        "last_cycle": asdict(report) if report else None,
        "providers_mode": c.cfg.providers.mode,
    }


@router.get("/equity")
async def equity(request: Request, mode: Literal["paper", "live"] | None = None,
                 days: int = Query(30, ge=1, le=365), _: Session = Depends(session)) -> dict[str, Any]:
    c = ctx(request).container
    m = TradeMode(mode) if mode else _book_mode(c)
    async with c.db.session() as s:
        rows = await EquityRepo(s).series(m, c.clock.now() - timedelta(days=days))
    return {"mode": m.value, "points": [{"ts": ser.iso(r.ts), "equity": round(r.equity_usd, 2),
                                         "exposure": round(r.exposure_usd, 2), "drawdown_pct": r.drawdown_pct}
                                        for r in rows]}


@router.get("/risk")
async def risk(request: Request, _: Session = Depends(session)) -> dict[str, Any]:
    c = ctx(request).container
    mode = _book_mode(c)
    book = await c.risk.book(mode)
    async with c.db.session() as s:
        events = await RiskEventRepo(s).recent(50)
    cfg = c.cfg
    return {
        "mode": mode.value, "book": book.to_dict(), "limits": asdict(c.risk.limits()) | {"level": int(c.mode.level)},
        "exits": cfg.exits.model_dump(mode="json"), "sizing": cfg.sizing.model_dump(mode="json"),
        "latency": cfg.latency.model_dump(mode="json"), "kill_switches": c.kill.snapshot(),
        "exposures": [asdict(e) for e in book.exposures],
        "events": [{"ts": ser.iso(e.ts), "type": e.type, "severity": e.severity, "message": e.message}
                   for e in events],
    }


class KillSwitchBody(BaseModel):
    action: Literal["activate", "deactivate"]
    scope: KillSwitchScope = KillSwitchScope.GLOBAL
    reason: str = Field("manual", max_length=300)
    flatten: bool = False
    password: str | None = Field(None, max_length=256)
    totp: str | None = Field(None, max_length=12)


@router.post("/risk/kill-switch")
async def kill_switch(body: KillSwitchBody, request: Request,
                      sess: Session = Depends(write_session)) -> dict[str, Any]:
    c = ctx(request).container
    ip = client_ip(request)
    if body.action == "activate":
        await c.kill.activate(body.scope, body.reason or "manual", actor=sess.username, ip=ip, flatten=body.flatten)
    else:
        if body.scope is KillSwitchScope.GLOBAL:
            await reauth(request, sess, body.password, body.totp)
        await c.kill.deactivate(body.scope, actor=sess.username, ip=ip)
    return c.kill.snapshot()


# --------------------------------------------------------------------- system
@router.get("/system/status")
async def system_status(request: Request, _: Session = Depends(session)) -> dict[str, Any]:
    c = ctx(request).container
    return {"mode": c.mode.status().to_dict(), "health": c.health.snapshot(),
            "overall": c.health.overall().value, "providers_mode": c.cfg.providers.mode,
            "last_cycle": asdict(c.cycle.last_report) if c.cycle.last_report else None,
            "notifications": [ch.name for ch in c.notifier.channels]}


@router.get("/system/preflight")
async def preflight(request: Request, _: Session = Depends(session)) -> dict[str, Any]:
    checks = await run_preflight(ctx(request).container)
    return {"passed": preflight_passed(checks), "checks": [
        {"name": ch.name, "label": ch.label, "passed": ch.passed, "critical": ch.critical, "message": ch.message}
        for ch in checks]}


class LevelBody(BaseModel):
    level: int = Field(ge=1, le=5)
    password: str | None = Field(None, max_length=256)
    totp: str | None = Field(None, max_length=12)


@router.post("/system/level")
async def set_level(body: LevelBody, request: Request, sess: Session = Depends(write_session)) -> dict[str, Any]:
    c = ctx(request).container
    await reauth(request, sess, body.password, body.totp)
    status = await c.mode.set_level(OperatingLevel(body.level), actor=sess.username, ip=client_ip(request))
    await c.refresh_tracking()
    return status.to_dict()


class ArmBody(BaseModel):
    password: str | None = Field(None, max_length=256)
    totp: str | None = Field(None, max_length=12)


@router.post("/system/arm")
async def arm(body: ArmBody, request: Request, sess: Session = Depends(write_session)) -> dict[str, Any]:
    c = ctx(request).container
    await reauth(request, sess, body.password, body.totp)
    checks = await run_preflight(c)
    if not preflight_passed(checks):
        failed = [f"{ch.label}: {ch.message}" for ch in checks if ch.critical and not ch.passed]
        raise HTTPException(status_code=409, detail="preflight no superado: " + "; ".join(failed))
    return (await c.mode.arm(actor=sess.username, ip=client_ip(request))).to_dict()


@router.post("/system/disarm")
async def disarm(request: Request, sess: Session = Depends(write_session)) -> dict[str, Any]:
    c = ctx(request).container
    return (await c.mode.disarm(actor=sess.username, ip=client_ip(request))).to_dict()


@router.post("/system/evaluate")
async def evaluate_now(request: Request, _: Session = Depends(write_session)) -> dict[str, bool]:
    app = ctx(request).application
    if app is not None:
        app.trigger_evaluation()
    return {"ok": True}


# --------------------------------------------------------------------- config
@router.get("/config")
async def get_config(request: Request, _: Session = Depends(session)) -> dict[str, Any]:
    svc = ctx(request).container.config_service
    return {"version": svc.version, "config": svc.current.model_dump(mode="json"), "overrides": svc.overrides,
            "mutable_sections": sorted(RUNTIME_MUTABLE_SECTIONS)}


class ConfigPatch(BaseModel):
    patch: dict[str, Any]
    comment: str = Field("", max_length=500)


@router.post("/config/preview")
async def preview_config(body: ConfigPatch, request: Request, _: Session = Depends(write_session)) -> dict[str, Any]:
    cfg = ctx(request).container.config_service.preview(body.patch)
    return {"valid": True, "config": cfg.model_dump(mode="json")}


@router.patch("/config")
async def patch_config(body: ConfigPatch, request: Request, sess: Session = Depends(write_session)) -> dict[str, Any]:
    c = ctx(request).container
    await c.config_service.apply_patch(body.patch, author=sess.username, comment=body.comment)
    async with c.db.session() as s:
        await AuditRepo(s).add(sess.username, "config_patch", None, {"patch": body.patch, "comment": body.comment},
                               client_ip(request))
    return {"version": c.config_service.version}


@router.get("/config/history")
async def config_history(request: Request, _: Session = Depends(session)) -> list[dict[str, Any]]:
    store = DbConfigStore(ctx(request).container.db)
    return [{"version": v.version, "author": v.author, "comment": v.comment, "created_at": ser.iso(v.created_at),
             "overrides": v.overrides} for v in await store.history()]


@router.post("/config/rollback/{version}")
async def rollback(version: int, request: Request, sess: Session = Depends(write_session)) -> dict[str, Any]:
    c = ctx(request).container
    await c.config_service.rollback(version, author=sess.username)
    async with c.db.session() as s:
        await AuditRepo(s).add(sess.username, "config_rollback", str(version), {}, client_ip(request))
    return {"version": c.config_service.version}


# ---------------------------------------------------------------- alerts/audit
@router.get("/alerts")
async def alerts(request: Request, limit: int = Query(100, ge=1, le=500), type: str | None = None,
                 severity: str | None = None, unacknowledged: bool = False, before: int | None = None,
                 _: Session = Depends(session)) -> list[dict[str, Any]]:
    async with ctx(request).container.db.session() as s:
        rows = await AlertRepo(s).list(limit=limit, type_=type, severity=severity, unacknowledged=unacknowledged,
                                       before_id=before)
    return [ser.alert(a) for a in rows]


class AckBody(BaseModel):
    id: int | None = None


@router.post("/alerts/ack")
async def ack(body: AckBody, request: Request, _: Session = Depends(write_session)) -> dict[str, bool]:
    async with ctx(request).container.db.session() as s:
        await AlertRepo(s).acknowledge(body.id)
    return {"ok": True}


@router.get("/audit")
async def audit(request: Request, _: Session = Depends(session)) -> list[dict[str, Any]]:
    async with ctx(request).container.db.session() as s:
        return [ser.audit(a) for a in await AuditRepo(s).list()]
