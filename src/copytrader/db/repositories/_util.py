"""Dialect-portable helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


def _insert_for(session: AsyncSession) -> Any:
    """Dialect-specific ``insert`` supporting ON CONFLICT DO NOTHING."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert
    raise RuntimeError(f"unsupported dialect {dialect}")  # pragma: no cover


async def insert_ignore(
    session: AsyncSession, model: Any, values: dict[str, Any], conflict_cols: list[str]
) -> int | None:
    """INSERT ... ON CONFLICT DO NOTHING RETURNING id.

    Returns the new id, or ``None`` if the row already existed. This is the
    primitive behind every idempotent write in the system.
    """
    insert = _insert_for(session)
    stmt = insert(model).values(**values).on_conflict_do_nothing(index_elements=conflict_cols).returning(model.id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def insert_many_ignore(
    session: AsyncSession, model: Any, rows: list[dict[str, Any]], conflict_cols: list[str], chunk: int = 500
) -> int:
    """Bulk INSERT ... ON CONFLICT DO NOTHING; returns how many rows were new."""
    if not rows:
        return 0
    insert = _insert_for(session)
    new = 0
    for i in range(0, len(rows), chunk):
        stmt = (
            insert(model)
            .values(rows[i : i + chunk])
            .on_conflict_do_nothing(index_elements=conflict_cols)
            .returning(model.id)
        )
        new += len((await session.execute(stmt)).scalars().all())
    return new
