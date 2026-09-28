"""Single-instance guard: only one trading process may use a given database.

Risk reservations and the "armed" flag live in process memory. Two processes
on the same database would each see only their own in-flight reservations and
could jointly exceed the exposure limits (order ids stay unique, so a signal is
never executed twice, but the limits would not hold). A second instance
therefore refuses to start.

* PostgreSQL: session-level advisory lock held on a dedicated autocommit
  connection; the server releases it if the process dies.
* SQLite: exclusive ``flock`` on ``<database>.lock``; the OS releases it on exit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text

from copytrader.core.errors import CopyTraderError
from copytrader.db.base import Database

log = structlog.get_logger(__name__)

# Arbitrary but stable 64-bit key shared by every copytrader process.
ADVISORY_LOCK_KEY = 0x436F_7079_5472_6164  # "CopyTrad"


class InstanceLockError(CopyTraderError):
    """Another copytrader process already uses this database."""


class InstanceLock:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._conn: Any = None
        self._fd: int | None = None

    async def acquire(self) -> None:
        if self.db.is_sqlite:
            self._acquire_file()
        else:
            await self._acquire_advisory()

    async def _acquire_advisory(self) -> None:
        conn = await self.db.engine.connect()
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY})).scalar()
        except BaseException:
            await conn.close()
            raise
        if not got:
            await conn.close()
            raise InstanceLockError(_BUSY)
        self._conn = conn

    def _acquire_file(self) -> None:
        path = self.db.url.split(":///", 1)[-1].split("?", 1)[0]
        if not path or path.startswith(":memory"):
            return
        try:
            import fcntl
        except ImportError:  # pragma: no cover  (Windows: no advisory file locks)
            log.warning("instance_lock_unavailable", reason="fcntl no disponible en este sistema")
            return
        fd = os.open(Path(path).with_name(Path(path).name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise InstanceLockError(_BUSY) from None
        self._fd = fd

    async def release(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
            finally:
                await self._conn.close()
                self._conn = None
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops the flock
            self._fd = None


_BUSY = (
    "otra instancia de copytrader ya está usando esta base de datos; "
    "ejecuta un solo proceso por base de datos (los límites de riesgo dependen de ello)"
)
