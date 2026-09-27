"""Wallet management and analytics endpoints."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from copytrader.api import serializers as ser
from copytrader.api.auth import Session
from copytrader.api.deps import client_ip, ctx, session, write_session
from copytrader.collector.wallet_collector import parse_list_type
from copytrader.core.types import ExitMode, ListType
from copytrader.db.repositories import AnalyticsRepo, AuditRepo, PositionRepo, TransactionRepo, WalletRepo

router = APIRouter(tags=["wallets"])


async def _after_change(request: Request) -> None:
    c = ctx(request)
    await c.container.refresh_tracking()
    if c.application is not None:
        c.application.trigger_evaluation()


@router.get("/wallets")
async def list_wallets(request: Request, status: str | None = None, list_type: str | None = None,
                       selected: bool | None = None, _: Session = Depends(session)) -> list[dict[str, Any]]:
    c = ctx(request).container
    async with c.db.session() as s:
        wallets = await WalletRepo(s).list()
        metrics = await AnalyticsRepo(s).latest_metrics_all("all")
    out = []
    for w in wallets:
        if status and w.status != status:
            continue
        if list_type and w.list_type != list_type:
            continue
        if selected is not None and w.selected != selected:
            continue
        out.append(ser.wallet(w, metrics.get(w.id)))
    return out


class WalletBody(BaseModel):
    address: str = Field(min_length=32, max_length=44)
    label: str | None = Field(None, max_length=100)
    list_type: str | None = Field(None, max_length=16)
    notes: str | None = Field(None, max_length=2000)


@router.post("/wallets")
async def add_wallet(body: WalletBody, request: Request, sess: Session = Depends(write_session)) -> dict[str, Any]:
    c = ctx(request).container
    created = await c.collector.add_wallet(body.address, label=body.label, notes=body.notes,
                                           list_type=parse_list_type(body.list_type) if body.list_type else None)
    async with c.db.session() as s:
        await AuditRepo(s).add(sess.username, "wallet_add", body.address, {"list_type": body.list_type},
                               client_ip(request))
    await _after_change(request)
    return {"created": created}


class ImportBody(BaseModel):
    csv: str = Field(max_length=200_000)


@router.post("/wallets/import")
async def import_wallets(body: ImportBody, request: Request, sess: Session = Depends(write_session)) -> dict[str, Any]:
    c = ctx(request).container
    report = await c.collector.import_csv(body.csv)
    async with c.db.session() as s:
        await AuditRepo(s).add(sess.username, "wallet_import", None,
                               {"added": len(report.added), "updated": len(report.updated),
                                "errors": len(report.errors)}, client_ip(request))
    await _after_change(request)
    return report.__dict__


@router.get("/wallets/{address}")
async def wallet_detail(address: str, request: Request, _: Session = Depends(session)) -> dict[str, Any]:
    c = ctx(request).container
    async with c.db.session() as s:
        w = await WalletRepo(s).get_by_address(address)
        if w is None:
            raise HTTPException(status_code=404, detail="wallet no encontrada")
        analytics = AnalyticsRepo(s)
        m_all = await analytics.latest_metrics(w.id, "all")
        m_recent = await analytics.latest_metrics(w.id, "recent")
        m_decayed = await analytics.latest_metrics(w.id, "decayed")
        flags = await analytics.active_flags(w.id)
        scores = await analytics.score_history(w.id, 200)
        txs = await TransactionRepo(s).recent_for_wallet(w.id, 50)
        n_tx = await TransactionRepo(s).count_for_wallet(w.id)
        positions = await PositionRepo(s).list(limit=50)
    return {
        "wallet": ser.wallet(w, m_all),
        "metrics": {"all": m_all.data if m_all else None, "recent": m_recent.data if m_recent else None,
                    "decayed": m_decayed.data if m_decayed else None},
        "flags": [ser.flag(f) for f in flags],
        "score": ser.score(scores[0]) if scores else None,
        "score_history": [{"ts": ser.iso(sc.computed_at), "score": sc.score, "status": sc.status}
                          for sc in reversed(scores)],
        "transactions": [{"signature": t.signature, "block_time": ser.iso(t.block_time), "side": t.side,
                          "token_mint": t.token_mint, "token_amount": t.token_amount, "price_usd": t.price_usd,
                          "value_usd": t.value_usd, "dex": t.dex, "source": t.source,
                          "detection_latency_ms": t.detection_latency_ms} for t in txs],
        "n_transactions": n_tx,
        "positions": [ser.position(p) for p in positions if p.source_wallet_id == w.id],
    }


class WalletPatch(BaseModel):
    label: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=2000)
    list_type: str | None = Field(None, max_length=16)
    exit_mode_override: str | None = Field(None, max_length=16)


@router.patch("/wallets/{address}")
async def patch_wallet(address: str, body: WalletPatch, request: Request,
                       sess: Session = Depends(write_session)) -> dict[str, Any]:
    c = ctx(request).container
    changes = body.model_dump(exclude_unset=True)
    async with c.db.session() as s:
        w = await WalletRepo(s).get_by_address(address)
        if w is None:
            raise HTTPException(status_code=404, detail="wallet no encontrada")
        if "label" in changes:
            w.label = body.label
        if "notes" in changes:
            w.notes = body.notes
        if "list_type" in changes:
            lt = parse_list_type(body.list_type)
            w.list_type = lt.value
            if lt is ListType.BLACKLIST:
                w.selected = False
        if "exit_mode_override" in changes:
            w.exit_mode_override = ExitMode(body.exit_mode_override).value if body.exit_mode_override else None
        await AuditRepo(s).add(sess.username, "wallet_update", address, changes, client_ip(request))
    await _after_change(request)
    return {"ok": True}


@router.delete("/wallets/{address}")
async def remove_wallet(address: str, request: Request, sess: Session = Depends(write_session)) -> dict[str, Any]:
    c = ctx(request).container
    await c.collector.untrack(address)
    async with c.db.session() as s:
        await AuditRepo(s).add(sess.username, "wallet_untrack", address, {}, client_ip(request))
    await _after_change(request)
    return {"ok": True}


@router.get("/selection")
async def selection(request: Request, _: Session = Depends(session)) -> dict[str, Any]:
    c = ctx(request).container
    async with c.db.session() as s:
        snap = await AnalyticsRepo(s).latest_selection()
        wallets = await WalletRepo(s).list()
    return {
        "top_n": c.cfg.selection.top_n, "min_score": c.cfg.selection.min_score,
        "computed_at": ser.iso(snap.computed_at) if snap else None,
        "selected": [ser.wallet(w) for w in sorted((w for w in wallets if w.selected),
                                                   key=lambda w: w.rank or 999)],
    }


@router.get("/tokens/{mint}")
async def token_info(mint: str, request: Request, refresh: bool = Query(False),
                     _: Session = Depends(session)) -> dict[str, Any]:
    c = ctx(request).container
    info = await c.tokens.get(mint, max_age_seconds=0 if refresh else None)
    data = asdict(info)
    data["fetched_at"] = ser.iso(info.fetched_at)
    data["pair_created_at"] = ser.iso(info.pair_created_at)
    return data
