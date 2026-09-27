"""Repositories for wallets, their transactions, metrics, scores and flags."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from copytrader.core.clock import utcnow
from copytrader.core.models import Flag, SwapEvent, TokenInfo
from copytrader.core.types import ListType, Side, TxSource
from copytrader.db.models import (
    SelectionSnapshot,
    Token,
    TokenSnapshot,
    Wallet,
    WalletFlag,
    WalletMetric,
    WalletScore,
    WalletTransaction,
)
from copytrader.db.repositories._util import insert_ignore, insert_many_ignore


class WalletRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def get(self, wallet_id: int) -> Wallet | None:
        return await self.s.get(Wallet, wallet_id)

    async def get_by_address(self, address: str) -> Wallet | None:
        return (await self.s.execute(select(Wallet).where(Wallet.address == address))).scalar_one_or_none()

    async def list(self, *, tracked_only: bool = True) -> Sequence[Wallet]:
        stmt = select(Wallet).order_by(Wallet.score.desc().nulls_last(), Wallet.id)
        if tracked_only:
            stmt = stmt.where(Wallet.is_tracked.is_(True))
        return (await self.s.execute(stmt)).scalars().all()

    async def count_tracked(self) -> int:
        stmt = select(func.count()).select_from(Wallet).where(Wallet.is_tracked.is_(True))
        return int((await self.s.execute(stmt)).scalar_one())

    async def upsert(
        self, address: str, *, label: str | None = None, list_type: ListType | None = None, notes: str | None = None
    ) -> tuple[Wallet, bool]:
        """Create or re-track a wallet. Returns (wallet, created)."""
        wallet = await self.get_by_address(address)
        created = wallet is None
        if wallet is None:
            wallet = Wallet(
                address=address,
                list_type=(list_type or ListType.NONE).value,
                label=label,
                notes=notes,
                is_tracked=True,
                added_at=utcnow(),
                status_reasons=["Pendiente de análisis"],
            )
            self.s.add(wallet)
        else:
            wallet.is_tracked = True
            if label is not None:
                wallet.label = label
            if list_type is not None:
                wallet.list_type = list_type.value
            if notes is not None:
                wallet.notes = notes
        await self.s.flush()
        return wallet, created

    async def touch_activity(self, wallet_id: int, ts: datetime, signature: str, slot: int) -> None:
        await self.s.execute(
            update(Wallet)
            .where(Wallet.id == wallet_id)
            .where((Wallet.last_seen_slot.is_(None)) | (Wallet.last_seen_slot <= slot))
            .values(last_activity_at=ts, last_seen_signature=signature, last_seen_slot=slot)
        )


class TransactionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def insert_swap(self, wallet_id: int, ev: SwapEvent) -> int | None:
        """Idempotent insert; returns None if this swap was already stored."""
        return await insert_ignore(
            self.s, WalletTransaction, swap_values(wallet_id, ev), ["wallet_id", "signature", "token_mint", "side"]
        )

    async def insert_swaps(self, wallet_id: int, swaps: Iterable[SwapEvent]) -> int:
        """Bulk idempotent insert; returns the number of new rows."""
        rows = [swap_values(wallet_id, ev) for ev in swaps]
        return await insert_many_ignore(
            self.s, WalletTransaction, rows, ["wallet_id", "signature", "token_mint", "side"]
        )

    async def swaps_for_wallet(
        self, wallet_id: int, address: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[SwapEvent]:
        stmt = select(WalletTransaction).where(WalletTransaction.wallet_id == wallet_id)
        if since is not None:
            stmt = stmt.where(WalletTransaction.block_time >= since)
        if until is not None:
            stmt = stmt.where(WalletTransaction.block_time < until)
        stmt = stmt.order_by(WalletTransaction.block_time, WalletTransaction.slot, WalletTransaction.id)
        rows = (await self.s.execute(stmt)).scalars().all()
        return [row_to_swap(r, address) for r in rows]

    async def all_swaps(
        self, *, since: datetime | None = None, until: datetime | None = None, wallet_ids: Iterable[int] | None = None
    ) -> list[tuple[int, SwapEvent]]:
        stmt = select(WalletTransaction, Wallet.address).join(Wallet, Wallet.id == WalletTransaction.wallet_id)
        if since is not None:
            stmt = stmt.where(WalletTransaction.block_time >= since)
        if until is not None:
            stmt = stmt.where(WalletTransaction.block_time < until)
        if wallet_ids is not None:
            stmt = stmt.where(WalletTransaction.wallet_id.in_(list(wallet_ids)))
        stmt = stmt.order_by(WalletTransaction.block_time, WalletTransaction.id)
        rows = (await self.s.execute(stmt)).all()
        return [(r[0].wallet_id, row_to_swap(r[0], r[1])) for r in rows]

    async def recent_for_wallet(self, wallet_id: int, limit: int = 50) -> Sequence[WalletTransaction]:
        stmt = (
            select(WalletTransaction)
            .where(WalletTransaction.wallet_id == wallet_id)
            .order_by(WalletTransaction.block_time.desc())
            .limit(limit)
        )
        return (await self.s.execute(stmt)).scalars().all()

    async def count_for_wallet(self, wallet_id: int) -> int:
        stmt = select(func.count()).select_from(WalletTransaction).where(WalletTransaction.wallet_id == wallet_id)
        return int((await self.s.execute(stmt)).scalar_one())

    async def distinct_mints(self, since: datetime | None = None) -> list[str]:
        stmt = select(WalletTransaction.token_mint).distinct()
        if since is not None:
            stmt = stmt.where(WalletTransaction.block_time >= since)
        return list((await self.s.execute(stmt)).scalars().all())


def swap_values(wallet_id: int, ev: SwapEvent) -> dict[str, Any]:
    return {
        "wallet_id": wallet_id,
        "signature": ev.signature,
        "slot": ev.slot,
        "block_time": ev.block_time,
        "token_mint": ev.token_mint,
        "side": ev.side.value,
        "token_amount": ev.token_amount,
        "token_decimals": ev.token_decimals,
        "quote_mint": ev.quote_mint,
        "quote_amount": ev.quote_amount,
        "price_quote": ev.price_quote,
        "price_usd": ev.price_usd,
        "value_usd": ev.value_usd,
        "sol_price_usd": ev.sol_price_usd,
        "fee_sol": ev.fee_sol,
        "dex": ev.dex,
        "token_balance_before": ev.token_balance_before,
        "token_balance_after": ev.token_balance_after,
        "liquidity_usd_at_trade": ev.liquidity_usd,
        "source": ev.source.value,
        "detected_at": ev.detected_at,
        "detection_latency_ms": ev.detection_latency_ms,
    }


def row_to_swap(r: WalletTransaction, address: str) -> SwapEvent:
    return SwapEvent(
        wallet=address,
        signature=r.signature,
        slot=r.slot,
        block_time=r.block_time,
        token_mint=r.token_mint,
        side=Side(r.side),
        token_amount=r.token_amount,
        token_decimals=r.token_decimals,
        quote_mint=r.quote_mint,
        quote_amount=r.quote_amount,
        price_quote=r.price_quote,
        price_usd=r.price_usd,
        value_usd=r.value_usd,
        sol_price_usd=r.sol_price_usd,
        fee_sol=r.fee_sol,
        dex=r.dex,
        token_balance_before=r.token_balance_before,
        token_balance_after=r.token_balance_after,
        source=TxSource(r.source),
        detected_at=r.detected_at,
        liquidity_usd=r.liquidity_usd_at_trade,
    )


class TokenRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def get(self, mint: str) -> Token | None:
        return (await self.s.execute(select(Token).where(Token.mint == mint))).scalar_one_or_none()

    async def get_many(self, mints: Iterable[str]) -> dict[str, Token]:
        mints = list(set(mints))
        if not mints:
            return {}
        rows = (await self.s.execute(select(Token).where(Token.mint.in_(mints)))).scalars().all()
        return {r.mint: r for r in rows}

    async def upsert(self, info: TokenInfo, *, snapshot: bool = True) -> None:
        await insert_ignore(self.s, Token, {"mint": info.mint, "updated_at": info.fetched_at}, ["mint"])
        token = await self.get(info.mint)
        assert token is not None
        token.symbol = info.symbol or token.symbol
        token.name = info.name or token.name
        token.decimals = info.decimals if info.decimals is not None else token.decimals
        token.category = info.category or token.category
        token.token_program = info.token_program or token.token_program
        token.mint_authority = info.mint_authority
        token.freeze_authority = info.freeze_authority
        if info.risk_score is not None:
            token.risk_score = info.risk_score
            token.risk_level = info.risk_level
            token.risk_flags = list(info.risk_flags)
        token.is_rugged = token.is_rugged or info.is_rugged
        token.pair_created_at = info.pair_created_at or token.pair_created_at
        token.last_price_usd = info.price_usd if info.price_usd is not None else token.last_price_usd
        token.last_liquidity_usd = info.liquidity_usd if info.liquidity_usd is not None else token.last_liquidity_usd
        token.last_market_cap_usd = (
            info.market_cap_usd if info.market_cap_usd is not None else token.last_market_cap_usd
        )
        token.updated_at = info.fetched_at
        if snapshot and (info.price_usd is not None or info.liquidity_usd is not None):
            self.s.add(
                TokenSnapshot(
                    mint=info.mint,
                    ts=info.fetched_at,
                    price_usd=info.price_usd,
                    liquidity_usd=info.liquidity_usd,
                    market_cap_usd=info.market_cap_usd,
                    volume_24h_usd=info.volume_24h_usd,
                )
            )

    async def rugged_mints(self) -> set[str]:
        rows = (await self.s.execute(select(Token.mint).where(Token.is_rugged.is_(True)))).scalars().all()
        return set(rows)


class AnalyticsRepo:
    """Metrics, scores, flags and selection snapshots."""

    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def add_metrics(self, wallet_id: int, window: str, data: dict[str, Any], computed_at: datetime) -> None:
        self.s.add(
            WalletMetric(
                wallet_id=wallet_id,
                window=window,
                computed_at=computed_at,
                n_trades=int(data.get("n_closed_trades") or 0),
                win_rate=data.get("win_rate"),
                profit_factor=_finite(data.get("profit_factor")),
                roi_pct=data.get("roi_pct"),
                max_drawdown_pct=data.get("max_drawdown_pct"),
                realized_pnl_usd=data.get("realized_pnl_usd"),
                unrealized_pnl_usd=data.get("unrealized_pnl_usd"),
                data=data,
            )
        )

    async def upsert_metrics(
        self, wallet_id: int, window: str, data: dict[str, Any], computed_at: datetime, snapshot_hours: float
    ) -> None:
        """Insert a new snapshot at most every ``snapshot_hours``; otherwise update the latest in place."""
        latest = await self.latest_metrics(wallet_id, window)
        if latest is None or (computed_at - latest.computed_at).total_seconds() >= snapshot_hours * 3600:
            await self.add_metrics(wallet_id, window, data, computed_at)
            return
        latest.n_trades = int(data.get("n_closed_trades") or 0)
        latest.win_rate = data.get("win_rate")
        latest.profit_factor = _finite(data.get("profit_factor"))
        latest.roi_pct = data.get("roi_pct")
        latest.max_drawdown_pct = data.get("max_drawdown_pct")
        latest.realized_pnl_usd = data.get("realized_pnl_usd")
        latest.unrealized_pnl_usd = data.get("unrealized_pnl_usd")
        latest.data = data
        latest.computed_at = computed_at

    async def add_score_throttled(self, score: WalletScore, min_minutes: float) -> None:
        """Keep score history compact: only store a new row on meaningful change or after ``min_minutes``."""
        latest = await self.latest_score(score.wallet_id)
        if (
            latest is not None
            and latest.status == score.status
            and latest.selected == score.selected
            and abs(latest.score - score.score) < 0.5
            and (score.computed_at - latest.computed_at).total_seconds() < min_minutes * 60
        ):
            for attr in (
                "score",
                "score_hist",
                "score_recent",
                "confidence",
                "components",
                "penalties",
                "status_reasons",
                "rank",
            ):
                setattr(latest, attr, getattr(score, attr))
            return
        self.s.add(score)

    async def latest_metrics(self, wallet_id: int, window: str = "all") -> WalletMetric | None:
        stmt = (
            select(WalletMetric)
            .where(WalletMetric.wallet_id == wallet_id, WalletMetric.window == window)
            .order_by(WalletMetric.computed_at.desc(), WalletMetric.id.desc())
            .limit(1)
        )
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def latest_metrics_all(self, window: str = "all") -> dict[int, WalletMetric]:
        sub = (
            select(WalletMetric.wallet_id, func.max(WalletMetric.id).label("mid"))
            .where(WalletMetric.window == window)
            .group_by(WalletMetric.wallet_id)
            .subquery()
        )
        stmt = select(WalletMetric).join(sub, WalletMetric.id == sub.c.mid)
        rows = (await self.s.execute(stmt)).scalars().all()
        return {r.wallet_id: r for r in rows}

    async def add_score(self, score: WalletScore) -> None:
        self.s.add(score)

    async def score_history(self, wallet_id: int, limit: int = 200) -> Sequence[WalletScore]:
        stmt = (
            select(WalletScore)
            .where(WalletScore.wallet_id == wallet_id)
            .order_by(WalletScore.computed_at.desc())
            .limit(limit)
        )
        return (await self.s.execute(stmt)).scalars().all()

    async def latest_score(self, wallet_id: int) -> WalletScore | None:
        rows = await self.score_history(wallet_id, 1)
        return rows[0] if rows else None

    async def replace_flags(self, wallet_id: int, flags: list[Flag], now: datetime) -> None:
        existing = {
            f.code: f
            for f in (await self.s.execute(select(WalletFlag).where(WalletFlag.wallet_id == wallet_id))).scalars().all()
        }
        seen: set[str] = set()
        for flag in flags:
            seen.add(flag.code)
            row = existing.get(flag.code)
            if row is None:
                self.s.add(
                    WalletFlag(
                        wallet_id=wallet_id,
                        code=flag.code,
                        severity=flag.severity.value,
                        message=flag.message,
                        evidence=flag.evidence,
                        active=True,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                if not row.active:
                    row.first_seen_at = now
                row.severity = flag.severity.value
                row.message = flag.message
                row.evidence = flag.evidence
                row.active = True
                row.last_seen_at = now
        for code, row in existing.items():
            if code not in seen and row.active:
                row.active = False

    async def active_flags(self, wallet_id: int) -> Sequence[WalletFlag]:
        stmt = select(WalletFlag).where(WalletFlag.wallet_id == wallet_id, WalletFlag.active.is_(True))
        return (await self.s.execute(stmt)).scalars().all()

    async def add_selection(self, snapshot: SelectionSnapshot) -> None:
        self.s.add(snapshot)

    async def latest_selection(self) -> SelectionSnapshot | None:
        stmt = select(SelectionSnapshot).order_by(SelectionSnapshot.id.desc()).limit(1)
        return (await self.s.execute(stmt)).scalar_one_or_none()


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f
