"""Only one trading process may use a database (risk reservations are per process)."""

from __future__ import annotations

import pytest

from copytrader.db.base import Database
from copytrader.db.instance_lock import InstanceLock, InstanceLockError
from tests.integration.conftest import PG_ADMIN, _pg_admin, db_url_for


async def test_second_instance_is_refused_until_the_first_releases(tmp_path):
    url = db_url_for(tmp_path)
    if PG_ADMIN:
        name = url.split("?")[0].rsplit("/", 1)[1]
        await _pg_admin(f"DROP DATABASE IF EXISTS {name}")
        await _pg_admin(f"CREATE DATABASE {name}")
    db1, db2 = Database(url), Database(url)
    first, second = InstanceLock(db1), InstanceLock(db2)
    try:
        await first.acquire()
        with pytest.raises(InstanceLockError, match="otra instancia"):
            await second.acquire()
        await first.release()
        await second.acquire()  # free again once the first process is gone
        await second.release()
    finally:
        await first.release()
        await db1.dispose()
        await db2.dispose()
