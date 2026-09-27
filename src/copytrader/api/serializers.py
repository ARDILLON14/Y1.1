"""ORM rows → JSON-friendly dicts for the API (never includes secrets)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from copytrader.core.types import LABELS_ES
from copytrader.db.models import (
    Alert,
    AuditLog,
    BacktestRun,
    Execution,
    Order,
    Position,
    Signal,
    Wallet,
    WalletFlag,
    WalletMetric,
    WalletScore,
)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def wallet(w: Wallet, metrics: WalletMetric | None = None) -> dict[str, Any]:
    d = metrics.data if metrics else {}
    return {
        "id": w.id,
        "address": w.address,
        "label": w.label,
        "notes": w.notes,
        "list_type": w.list_type,
        "status": w.status,
        "status_label": LABELS_ES.get(w.status, w.status),
        "status_reasons": w.status_reasons,
        "score": w.score,
        "rank": w.rank,
        "selected": w.selected,
        "exit_mode_override": w.exit_mode_override,
        "added_at": iso(w.added_at),
        "last_activity_at": iso(w.last_activity_at),
        "analyzed_at": iso(w.analyzed_at),
        "backfilled": w.backfilled_at is not None,
        "metrics": {
            "n_trades": d.get("n_closed_trades"),
            "win_rate": d.get("win_rate"),
            "profit_factor": d.get("profit_factor"),
            "roi_pct": d.get("roi_pct"),
            "realized_pnl_usd": d.get("realized_pnl_usd"),
            "unrealized_pnl_usd": d.get("unrealized_pnl_usd"),
            "total_pnl_usd": d.get("total_pnl_usd"),
            "max_drawdown_pct": d.get("max_drawdown_pct"),
            "trades_per_day": d.get("trades_per_day"),
            "avg_holding_minutes": d.get("avg_holding_minutes"),
        }
        if metrics
        else None,
    }


def flag(f: WalletFlag) -> dict[str, Any]:
    return {
        "code": f.code,
        "severity": f.severity,
        "message": f.message,
        "evidence": f.evidence,
        "first_seen_at": iso(f.first_seen_at),
        "last_seen_at": iso(f.last_seen_at),
    }


def score(sc: WalletScore) -> dict[str, Any]:
    return {
        "computed_at": iso(sc.computed_at),
        "score": sc.score,
        "score_hist": sc.score_hist,
        "score_recent": sc.score_recent,
        "confidence": sc.confidence,
        "components": sc.components,
        "penalties": sc.penalties,
        "status": sc.status,
        "status_reasons": sc.status_reasons,
        "rank": sc.rank,
        "selected": sc.selected,
    }


def signal(s: Signal, wallet_row: Wallet | None = None) -> dict[str, Any]:
    return {
        "id": s.id,
        "trace_id": s.trace_id,
        "wallet_id": s.wallet_id,
        "wallet": wallet_row.address if wallet_row else None,
        "wallet_label": wallet_row.label if wallet_row else None,
        "source_signature": s.source_signature,
        "token_mint": s.token_mint,
        "token_symbol": s.token_symbol,
        "side": s.side,
        "action": s.action,
        "status": s.status,
        "reason": s.reason,
        "source_price_usd": s.source_price_usd,
        "source_value_usd": s.source_value_usd,
        "source_block_time": iso(s.source_block_time),
        "detected_at": iso(s.detected_at),
        "decided_at": iso(s.decided_at),
        "detection_latency_ms": s.detection_latency_ms,
        "wallet_score": s.wallet_score,
        "operating_level": s.operating_level,
        "mode": s.mode,
        "decision": s.decision,
    }


def execution(e: Execution, o: Order) -> dict[str, Any]:
    ctx = o.context or {}
    return {
        "id": e.id,
        "order_id": o.id,
        "client_order_id": o.client_order_id,
        "signal_id": o.signal_id,
        "position_id": o.position_id,
        "mode": e.mode,
        "purpose": o.purpose,
        "side": e.side,
        "token_mint": e.token_mint,
        "token_symbol": ctx.get("token_symbol"),
        "source_wallet": ctx.get("source_wallet"),
        "tx_signature": e.tx_signature,
        "signal_price_usd": e.signal_price_usd,
        "theoretical_price_usd": e.theoretical_price_usd,
        "quote_price_usd": e.quote_price_usd,
        "fill_price_usd": e.fill_price_usd,
        "slippage_bps": e.slippage_bps,
        "price_impact_bps": e.price_impact_bps,
        "value_usd": e.value_usd,
        "fees_usd": e.fees_usd,
        "token_qty": e.token_qty,
        "realized_pnl_usd": e.realized_pnl_usd,
        "latency_ms": e.latency_ms,
        "executed_at": iso(e.executed_at),
        "reason": ctx.get("reason"),
        "trigger": o.trigger,
    }


def order(o: Order) -> dict[str, Any]:
    return {
        "id": o.id,
        "client_order_id": o.client_order_id,
        "mode": o.mode,
        "purpose": o.purpose,
        "side": o.side,
        "token_mint": o.token_mint,
        "status": o.status,
        "notional_usd": o.notional_usd,
        "tx_signature": o.tx_signature,
        "error": o.error,
        "trigger": o.trigger,
        "created_at": iso(o.created_at),
        "updated_at": iso(o.updated_at),
    }


def position(p: Position, wallet_row: Wallet | None = None) -> dict[str, Any]:
    qty = p.qty_raw / 10**p.decimals if p.decimals is not None else None
    value = qty * p.last_price_usd if qty is not None and p.last_price_usd else None
    unrealized = (value - p.cost_usd) if value is not None and p.status != "closed" else None
    return {
        "id": p.id,
        "mode": p.mode,
        "token_mint": p.token_mint,
        "token_symbol": p.token_symbol,
        "source_wallet": wallet_row.address if wallet_row else None,
        "source_wallet_label": wallet_row.label if wallet_row else None,
        "exit_mode": p.exit_mode,
        "exit_mode_label": LABELS_ES.get(p.exit_mode, p.exit_mode),
        "status": p.status,
        "qty": qty,
        "cost_usd": round(p.cost_usd, 4),
        "initial_cost_usd": round(p.initial_cost_usd, 4),
        "entry_price_usd": p.entry_price_usd,
        "last_price_usd": p.last_price_usd,
        "peak_price_usd": p.peak_price_usd,
        "value_usd": value,
        "unrealized_pnl_usd": unrealized,
        "realized_pnl_usd": round(p.realized_pnl_usd, 4),
        "change_pct": (p.last_price_usd / p.entry_price_usd - 1) * 100
        if p.last_price_usd and p.entry_price_usd
        else None,
        "fees_usd": p.fees_usd,
        "tp_levels_hit": p.tp_levels_hit,
        "is_high_risk": p.is_high_risk,
        "category": p.category,
        "at_risk_usd": p.at_risk_usd,
        "opened_at": iso(p.opened_at),
        "closed_at": iso(p.closed_at),
        "close_reason": p.close_reason,
        "last_price_at": iso(p.last_price_at),
    }


def alert(a: Alert) -> dict[str, Any]:
    return {
        "id": a.id,
        "ts": iso(a.ts),
        "type": a.type,
        "severity": a.severity,
        "title": a.title,
        "body": a.body,
        "channels": a.channels,
        "acknowledged": a.acknowledged,
    }


def audit(a: AuditLog) -> dict[str, Any]:
    return {
        "id": a.id,
        "ts": iso(a.ts),
        "actor": a.actor,
        "action": a.action,
        "target": a.target,
        "data": a.data,
        "ip": a.ip,
    }


def backtest(b: BacktestRun, full: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": b.id,
        "created_at": iso(b.created_at),
        "status": b.status,
        "params": b.params,
        "error": b.error,
    }
    if full:
        out["results"] = b.results
    elif b.results and "results" in b.results:
        out["summary"] = {
            k: {kk: vv for kk, vv in v.items() if kk != "equity_curve"} for k, v in b.results["results"].items()
        }
    return out
