"""Wallet Collector: registration, lists, CSV import, backfill and token refresh."""

from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta

import structlog

from copytrader.config.models import AppConfig
from copytrader.core.clock import Clock
from copytrader.core.errors import CopyTraderError
from copytrader.core.models import SwapEvent
from copytrader.core.types import ListType, TxSource
from copytrader.db.base import Database
from copytrader.db.repositories import TokenRepo, TransactionRepo, WalletRepo
from copytrader.providers.interfaces import HistorySource, TokenInfoProvider
from copytrader.providers.solana.constants import is_valid_address

log = structlog.get_logger(__name__)

_LIST_ALIASES = {
    "": ListType.NONE,
    "none": ListType.NONE,
    "-": ListType.NONE,
    "white": ListType.WHITELIST,
    "whitelist": ListType.WHITELIST,
    "watch": ListType.WATCHLIST,
    "watchlist": ListType.WATCHLIST,
    "black": ListType.BLACKLIST,
    "blacklist": ListType.BLACKLIST,
}


@dataclass
class ImportReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_list_type(value: str | None) -> ListType:
    key = (value or "").strip().lower()
    if key not in _LIST_ALIASES:
        raise ValueError(f"lista desconocida '{value}' (usa whitelist/watchlist/blacklist)")
    return _LIST_ALIASES[key]


class WalletCollector:
    def __init__(
        self,
        db: Database,
        history: HistorySource,
        tokens: TokenInfoProvider,
        clock: Clock,
        config: Callable[[], AppConfig],
        address_validator: Callable[[str], bool] = is_valid_address,
    ) -> None:
        self.db = db
        self.history = history
        self.tokens = tokens
        self.clock = clock
        self._config = config
        self._validate = address_validator
        self._backfill_sem = asyncio.Semaphore(config().providers.solana.backfill_concurrency)

    # ------------------------------------------------------------ registration
    async def add_wallet(
        self, address: str, *, label: str | None = None, list_type: ListType | None = None, notes: str | None = None
    ) -> bool:
        address = address.strip()
        if not self._validate(address):
            raise ValueError(f"dirección inválida: {address!r}")
        async with self.db.session() as s:
            repo = WalletRepo(s)
            existing = await repo.get_by_address(address)
            if (
                existing is None or not existing.is_tracked
            ) and await repo.count_tracked() >= self._config().wallets.max_wallets:
                raise ValueError(f"límite de wallets alcanzado ({self._config().wallets.max_wallets})")
            _, created = await repo.upsert(address, label=label, list_type=list_type, notes=notes)
        return created

    async def import_csv(self, text: str) -> ImportReport:
        """CSV columns: ``address[,label[,list[,notes]]]``; header optional; ``#`` comments."""
        report = ImportReport()
        rows = [r for r in csv.reader(io.StringIO(text)) if r and not r[0].strip().startswith("#")]
        if rows and rows[0][0].strip().lower() in ("address", "wallet", "direccion", "dirección"):
            rows = rows[1:]
        for n, row in enumerate(rows, start=1):
            address = row[0].strip()
            label = row[1].strip() if len(row) > 1 and row[1].strip() else None
            try:
                list_type = parse_list_type(row[2]) if len(row) > 2 else None
                notes = row[3].strip() if len(row) > 3 and row[3].strip() else None
                created = await self.add_wallet(address, label=label, list_type=list_type, notes=notes)
                (report.added if created else report.updated).append(address)
            except ValueError as exc:
                report.errors.append(f"línea {n}: {exc}")
        return report

    async def set_list(self, address: str, list_type: ListType) -> None:
        async with self.db.session() as s:
            wallet = await WalletRepo(s).get_by_address(address)
            if wallet is None:
                raise ValueError("wallet no encontrada")
            wallet.list_type = list_type.value
            if list_type is ListType.BLACKLIST:
                wallet.selected = False

    async def untrack(self, address: str) -> None:
        async with self.db.session() as s:
            wallet = await WalletRepo(s).get_by_address(address)
            if wallet is None:
                raise ValueError("wallet no encontrada")
            wallet.is_tracked = False
            wallet.selected = False

    async def cursor(self, address: str) -> str | None:
        async with self.db.session() as s:
            wallet = await WalletRepo(s).get_by_address(address)
            return wallet.last_seen_signature if wallet else None

    # ------------------------------------------------------------------ storage
    async def store_swaps(self, wallet_id: int, swaps: Iterable[SwapEvent]) -> int:
        """Idempotently persist swaps; returns how many were new."""
        swaps = list(swaps)
        latest = max(swaps, key=lambda sw: (sw.slot, sw.block_time), default=None)
        async with self.db.session() as s:
            new = await TransactionRepo(s).insert_swaps(wallet_id, swaps)
            if latest is not None:
                await WalletRepo(s).touch_activity(wallet_id, latest.block_time, latest.signature, latest.slot)
        return new

    # ----------------------------------------------------------------- backfill
    async def backfill(self, address: str, *, force: bool = False) -> int:
        cfg = self._config()
        async with self.db.session() as s:
            wallet = await WalletRepo(s).get_by_address(address)
            if wallet is None:
                raise ValueError("wallet no encontrada")
            wallet_id, already = wallet.id, wallet.backfilled_at is not None
        if already and not force:
            return 0
        since = self.clock.now() - timedelta(days=cfg.analysis.history_days)
        async with self._backfill_sem:
            try:
                swaps = await self.history.fetch_swaps(
                    address,
                    since=since,
                    max_signatures=cfg.providers.solana.backfill_max_signatures_per_wallet,
                    source=TxSource.BACKFILL,
                )
            except CopyTraderError as exc:
                log.warning("backfill_failed", wallet=address, error=str(exc))
                return 0
        new = await self.store_swaps(wallet_id, swaps)
        async with self.db.session() as s:
            wallet = await WalletRepo(s).get(wallet_id)
            if wallet is not None:
                wallet.backfilled_at = self.clock.now()
        log.info("backfill_done", wallet=address, swaps=len(swaps), new=new)
        await self.refresh_tokens({sw.token_mint for sw in swaps})
        return new

    async def backfill_all(self, *, force: bool = False) -> dict[str, int]:
        async with self.db.session() as s:
            wallets = [w.address for w in await WalletRepo(s).list()]
        results = await asyncio.gather(*(self.backfill(a, force=force) for a in wallets), return_exceptions=True)
        out: dict[str, int] = {}
        for address, result in zip(wallets, results, strict=True):
            if isinstance(result, BaseException):
                log.error("backfill_error", wallet=address, error=repr(result))
                out[address] = -1
            else:
                out[address] = result
        return out

    async def refresh_tokens(self, mints: Iterable[str], batch: int = 30) -> None:
        mints = sorted(set(mints))
        for i in range(0, len(mints), batch):
            chunk = mints[i : i + batch]
            try:
                infos = await self.tokens.get_many(chunk)
            except CopyTraderError as exc:
                log.warning("token_refresh_failed", error=str(exc))
                continue
            async with self.db.session() as s:
                repo = TokenRepo(s)
                for info in infos.values():
                    await repo.upsert(info)
