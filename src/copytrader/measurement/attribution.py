"""Where do the results come from, and where do they go?

One report, per book (paper or live) and period:

* ``filters``  — forward returns of rejected signals grouped by the check that
                 rejected them, next to the executed ones: which filters protect
                 the capital and which ones only cost winners;
* ``wallets``  — realized result of copying each wallet vs the copy estimate;
* ``exits``    — realized PnL by exit trigger (stop, take profit, source sell…);
* ``delay``    — result by how late we entered after the source;
* ``costs``    — network fees vs gross result, and the entry slippage split into
                 "arriving late" (signal → quote) and "executing" (quote → fill).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from statistics import mean, median
from typing import Any

from sqlalchemy import select

from copytrader.core.types import OrderPurpose, PositionStatus, SignalStatus
from copytrader.db.base import Database
from copytrader.db.models import Execution, Order, Position, Signal, SignalOutcome, Wallet
from copytrader.db.repositories import AnalyticsRepo
from copytrader.measurement.outcomes import horizon_key

MIN_SAMPLE = 10
DELAY_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 2.0, "< 2 s"),
    (2.0, 5.0, "2-5 s"),
    (5.0, 10.0, "5-10 s"),
    (10.0, 20.0, "10-20 s"),
    (20.0, float("inf"), "> 20 s"),
)


def _pct(values: Sequence[float]) -> float | None:
    return 100 * mean(values) if values else None


def _horizon_stats(rows: Sequence[SignalOutcome], horizons: Sequence[float]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for h in horizons:
        k = horizon_key(h)
        values = [r.returns[k] for r in rows if isinstance((r.returns or {}).get(k), (int, float))]
        out[k] = {
            "n": len(values),
            "mean_pct": _pct(values),
            "median_pct": 100 * median(values) if values else None,
            "up_frac": sum(1 for v in values if v > 0) / len(values) if values else None,
        }
    return out


def verdict(group: dict[str, Any], executed: dict[str, Any] | None, key: str) -> str:
    """Plain-language reading of a rejection group at the reference horizon."""
    stats = group["horizons"].get(key) or {}
    if (stats.get("n") or 0) < MIN_SAMPLE or stats.get("mean_pct") is None:
        return "Muestra insuficiente"
    base = ((executed or {}).get("horizons") or {}).get(key) or {}
    if stats["mean_pct"] < 0:
        return "Protege: lo rechazado baja de media"
    if base.get("mean_pct") is not None and (base.get("n") or 0) >= MIN_SAMPLE and stats["mean_pct"] > base["mean_pct"]:
        return "Revisar: lo rechazado sube más que lo ejecutado"
    return "Neutral"


class AttributionService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def report(self, *, mode: str, since: datetime, horizons: Sequence[float]) -> dict[str, Any]:
        async with self.db.session() as s:
            outcomes = (
                (await s.execute(select(SignalOutcome).where(SignalOutcome.reference_at >= since))).scalars().all()
            )
            positions = (
                (
                    await s.execute(
                        select(Position).where(
                            Position.mode == mode,
                            Position.status == PositionStatus.CLOSED.value,
                            Position.closed_at >= since,
                        )
                    )
                )
                .scalars()
                .all()
            )
            exits = (
                await s.execute(
                    select(Execution, Order)
                    .join(Order, Order.id == Execution.order_id)
                    .where(
                        Execution.mode == mode,
                        Order.purpose == OrderPurpose.EXIT.value,
                        Execution.executed_at >= since,
                    )
                )
            ).all()
            entries = (
                await s.execute(
                    select(Execution, Order, Signal)
                    .join(Order, Order.id == Execution.order_id)
                    .join(Signal, Signal.id == Order.signal_id)
                    .where(
                        Execution.mode == mode,
                        Order.purpose == OrderPurpose.ENTRY.value,
                        Execution.executed_at >= since,
                    )
                )
            ).all()
            wallets = {w.id: w for w in (await s.execute(select(Wallet))).scalars().all()}
            metrics = await AnalyticsRepo(s).latest_metrics_all("all")

        return {
            "mode": mode,
            "since": since.isoformat(),
            "horizons": [horizon_key(h) for h in horizons],
            "filters": self._filters(outcomes, horizons),
            "wallets": self._wallets(positions, wallets, metrics),
            "exits": self._exits(exits),
            "delay": self._delay(entries, positions),
            "costs": self._costs(positions, entries),
        }

    # ------------------------------------------------------------------ parts
    @staticmethod
    def _filters(outcomes: Sequence[SignalOutcome], horizons: Sequence[float]) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], list[SignalOutcome]] = defaultdict(list)
        for o in outcomes:
            if o.status == SignalStatus.EXECUTED.value:
                groups[("executed", "Ejecutadas (referencia)")].append(o)
            else:
                groups[(o.failed_check or o.status, o.failed_label or o.status)].append(o)
        ref = min(horizons, key=lambda h: abs(h - 60)) if horizons else 60
        key = horizon_key(ref)
        rows: list[dict[str, Any]] = [
            {"check": check, "label": label, "n": len(items), "horizons": _horizon_stats(items, horizons)}
            for (check, label), items in groups.items()
        ]
        executed = next((r for r in rows if r["check"] == "executed"), None)
        for r in rows:
            r["verdict"] = "" if r["check"] == "executed" else verdict(r, executed, key)
        rows.sort(key=lambda r: (r["check"] != "executed", -r["n"]))
        return rows

    @staticmethod
    def _wallets(
        positions: Sequence[Position], wallets: dict[int, Wallet], metrics: dict[int, Any]
    ) -> list[dict[str, Any]]:
        by_wallet: dict[int | None, list[Position]] = defaultdict(list)
        for p in positions:
            by_wallet[p.source_wallet_id].append(p)
        rows: list[dict[str, Any]] = []
        for wid, items in by_wallet.items():
            returns = [p.realized_pnl_usd / p.initial_cost_usd for p in items if p.initial_cost_usd]
            w = wallets.get(wid) if wid is not None else None
            data = metrics[wid].data if wid in metrics else {}
            rows.append(
                {
                    "wallet": w.address if w else None,
                    "label": w.label if w else None,
                    "n": len(items),
                    "pnl_usd": sum(p.realized_pnl_usd for p in items),
                    "fees_usd": sum(p.fees_usd for p in items),
                    "win_rate": sum(1 for p in items if p.realized_pnl_usd > 0) / len(items),
                    "avg_return_pct": _pct(returns),
                    "estimated_copy_pct": data.get("copy_expectancy_pct"),
                    "wallet_expectancy_pct": data.get("expectancy_pct"),
                }
            )
        rows.sort(key=lambda r: r["pnl_usd"], reverse=True)
        return rows

    @staticmethod
    def _exits(exits: Sequence[Any]) -> list[dict[str, Any]]:
        groups: dict[str, list[Execution]] = defaultdict(list)
        for ex, order in exits:
            trigger = order.trigger or "desconocido"
            if trigger.startswith("take_profit"):
                trigger = "take_profit"
            groups[trigger].append(ex)
        rows: list[dict[str, Any]] = [
            {
                "trigger": trigger,
                "n": len(items),
                "pnl_usd": sum(e.realized_pnl_usd or 0.0 for e in items),
                "win_rate": sum(1 for e in items if (e.realized_pnl_usd or 0.0) > 0) / len(items),
            }
            for trigger, items in groups.items()
        ]
        rows.sort(key=lambda r: r["n"], reverse=True)
        return rows

    @staticmethod
    def _delay(entries: Sequence[Any], positions: Sequence[Position]) -> list[dict[str, Any]]:
        closed = {p.id: p for p in positions}
        buckets: dict[str, list[float]] = {label: [] for _, _, label in DELAY_BUCKETS}
        counts: dict[str, int] = dict.fromkeys(buckets, 0)
        for ex, order, sig in entries:
            delay = (ex.executed_at - sig.source_block_time).total_seconds()
            label = next(lbl for lo, hi, lbl in DELAY_BUCKETS if lo <= max(delay, 0.0) < hi)
            counts[label] += 1
            pos = closed.get(order.position_id) if order.position_id else None
            if pos is not None and pos.initial_cost_usd:
                buckets[label].append(pos.realized_pnl_usd / pos.initial_cost_usd)
        return [
            {
                "bucket": label,
                "entries": counts[label],
                "closed": len(buckets[label]),
                "avg_return_pct": _pct(buckets[label]),
                "win_rate": (sum(1 for v in buckets[label] if v > 0) / len(buckets[label])) if buckets[label] else None,
            }
            for _, _, label in DELAY_BUCKETS
        ]

    @staticmethod
    def _costs(positions: Sequence[Position], entries: Sequence[Any]) -> dict[str, Any]:
        net = sum(p.realized_pnl_usd for p in positions)
        fees = sum(p.fees_usd for p in positions)
        gross = net + fees
        late = [
            ex.quote_price_usd / ex.signal_price_usd - 1
            for ex, _, _ in entries
            if ex.quote_price_usd and ex.signal_price_usd
        ]
        execution = [
            ex.fill_price_usd / ex.quote_price_usd - 1
            for ex, _, _ in entries
            if ex.fill_price_usd and ex.quote_price_usd
        ]
        return {
            "positions": len(positions),
            "gross_pnl_usd": gross,
            "fees_usd": fees,
            "net_pnl_usd": net,
            "fees_share_of_gross_pct": 100 * fees / abs(gross) if gross else None,
            "entry_late_pct": _pct(late),
            "entry_execution_pct": _pct(execution),
            "entries_measured": len(late),
        }
