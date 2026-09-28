"""Signals, trades, orders, positions and backtests."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from copytrader.api import serializers as ser
from copytrader.api.auth import Session
from copytrader.api.deps import client_ip, ctx, session, write_session
from copytrader.core.models import CheckResult, Decision
from copytrader.db.repositories import (
    AuditRepo,
    BacktestRepo,
    EventLogRepo,
    ExecutionRepo,
    OrderRepo,
    PositionRepo,
    SignalRepo,
    WalletRepo,
)

router = APIRouter(tags=["trading"])
_backtests: set[asyncio.Task[Any]] = set()


async def _wallet_map(c: Any) -> dict[int, Any]:
    async with c.db.session() as s:
        return {w.id: w for w in await WalletRepo(s).list(tracked_only=False)}


@router.get("/signals")
async def list_signals(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    status: str | None = None,
    action: str | None = None,
    before: int | None = None,
    _: Session = Depends(session),
) -> list[dict[str, Any]]:
    c = ctx(request).container
    async with c.db.session() as s:
        rows = await SignalRepo(s).list(limit=limit, status=status, action=action, before_id=before)
    wallets = await _wallet_map(c)
    return [ser.signal(r, wallets.get(r.wallet_id)) for r in rows]


@router.get("/signals/{signal_id}")
async def signal_detail(signal_id: int, request: Request, _: Session = Depends(session)) -> dict[str, Any]:
    c = ctx(request).container
    async with c.db.session() as s:
        row = await SignalRepo(s).get(signal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="señal no encontrada")
        events = await EventLogRepo(s).by_trace(row.trace_id)
    wallets = await _wallet_map(c)
    data = ser.signal(row, wallets.get(row.wallet_id))
    dec = row.decision or {}
    if dec.get("checks"):
        decision = Decision(
            approved=bool(dec.get("approved")),
            reason=dec.get("reason"),
            checks=[
                CheckResult(
                    ch["name"],
                    ch["label"],
                    ch["passed"],
                    ch.get("value"),
                    ch.get("limit"),
                    ch.get("message", ""),
                    ch.get("critical", True),
                )
                for ch in dec["checks"]
            ],
        )
        w = wallets.get(row.wallet_id)
        header = (
            f"Wallet {w.label or w.address[:6] if w else '?'}\nScore: {row.wallet_score or 0:.0f}\n\n"
            f"{row.side.upper()} DETECTED"
        )
        data["explanation"] = decision.explain(header)
    data["events"] = [{"ts": ser.iso(e.ts), "component": e.component, "event": e.event, "data": e.data} for e in events]
    return data


@router.get("/trades")
async def trades(
    request: Request,
    mode: Literal["paper", "live"] | None = None,
    limit: int = Query(100, ge=1, le=500),
    before: int | None = None,
    _: Session = Depends(session),
) -> list[dict[str, Any]]:
    async with ctx(request).container.db.session() as s:
        rows = await ExecutionRepo(s).list(limit=limit, mode=mode, before_id=before)
    return [ser.execution(e, o) for e, o in rows]


@router.get("/orders")
async def orders(
    request: Request,
    mode: Literal["paper", "live"] | None = None,
    limit: int = Query(100, ge=1, le=500),
    _: Session = Depends(session),
) -> list[dict[str, Any]]:
    async with ctx(request).container.db.session() as s:
        return [ser.order(o) for o in await OrderRepo(s).list(limit=limit, mode=mode)]


@router.get("/positions")
async def positions(
    request: Request,
    status: str | None = Query(None, pattern="^(open|closed|closing)$"),
    mode: Literal["paper", "live"] | None = None,
    limit: int = Query(200, ge=1, le=1000),
    _: Session = Depends(session),
) -> list[dict[str, Any]]:
    c = ctx(request).container
    async with c.db.session() as s:
        rows = await PositionRepo(s).list(status=status, mode=mode, limit=limit)
    wallets = await _wallet_map(c)
    return [ser.position(p, wallets.get(p.source_wallet_id or -1)) for p in rows]


class CloseBody(BaseModel):
    reason: str = Field("Cierre manual", max_length=200)


@router.post("/positions/{position_id}/close")
async def close_position(
    position_id: int, body: CloseBody, request: Request, sess: Session = Depends(write_session)
) -> dict[str, Any]:
    c = ctx(request).container
    async with c.db.session() as s:
        await AuditRepo(s).add(
            sess.username, "position_close", str(position_id), {"reason": body.reason}, client_ip(request)
        )
    result = await c.positions.close_position(position_id, reason=f"{body.reason} ({sess.username})")
    if result is None:
        raise HTTPException(status_code=409, detail="la posición no está abierta o ya se está cerrando")
    return {"success": result.success, "error": result.error, "value_usd": result.value_usd}


class BacktestVariant(BaseModel):
    name: str = Field("", max_length=40)
    patch: dict[str, Any]


class BacktestBody(BaseModel):
    train_days: int | None = Field(None, ge=1, le=365)
    test_days: int | None = Field(None, ge=1, le=90)
    top_n: int | None = Field(None, ge=1, le=100)
    latency_seconds: float | None = Field(None, ge=0, le=600)
    entry_slippage_pct: float | None = Field(None, ge=0, le=50)
    exit_slippage_pct: float | None = Field(None, ge=0, le=50)
    exit_mode: Literal["mirror", "protected", "smart"] | None = None
    # Alternative configurations run on the same data: [{"name": ..., "patch": {...}}]
    variants: list[BacktestVariant] | None = Field(None, max_length=2)


@router.post("/backtest")
async def start_backtest(
    body: BacktestBody, request: Request, sess: Session = Depends(write_session)
) -> dict[str, Any]:
    from copytrader.backtest.service import run_backtest, validate_variants

    c = ctx(request).container
    if any(not t.done() for t in _backtests):
        raise HTTPException(status_code=409, detail="ya hay un backtest en curso")
    variants = [v.model_dump() for v in body.variants or []]
    validate_variants(c, variants)  # invalid patches fail here (400), not inside the background task
    overrides = body.model_dump(exclude_none=True, exclude={"variants"})
    task = asyncio.get_running_loop().create_task(run_backtest(c, overrides, variants))
    _backtests.add(task)
    task.add_done_callback(_backtests.discard)
    async with c.db.session() as s:
        await AuditRepo(s).add(
            sess.username, "backtest_start", None, body.model_dump(exclude_none=True), client_ip(request)
        )
    return {"started": True}


@router.get("/backtest")
async def list_backtests(request: Request, _: Session = Depends(session)) -> list[dict[str, Any]]:
    async with ctx(request).container.db.session() as s:
        return [ser.backtest(b) for b in await BacktestRepo(s).list()]


@router.get("/backtest/{run_id}")
async def get_backtest(run_id: int, request: Request, _: Session = Depends(session)) -> dict[str, Any]:
    async with ctx(request).container.db.session() as s:
        run = await BacktestRepo(s).get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="backtest no encontrado")
    return ser.backtest(run, full=True)
