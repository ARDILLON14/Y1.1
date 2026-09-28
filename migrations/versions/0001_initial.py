"""Initial schema (baseline).

The baseline is created from the ORM metadata so it matches the models exactly;
later schema changes are generated with ``alembic revision --autogenerate``.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables added by later revisions: the baseline must not create them, or those
# revisions would fail on a fresh database.
LATER_TABLES = frozenset({"signal_outcomes"})


def upgrade() -> None:
    from copytrader.db import models  # noqa: F401
    from copytrader.db.base import Base

    tables = [t for name, t in Base.metadata.tables.items() if name not in LATER_TABLES]
    Base.metadata.create_all(op.get_bind(), tables=tables)


def downgrade() -> None:
    from copytrader.db import models  # noqa: F401
    from copytrader.db.base import Base

    Base.metadata.drop_all(op.get_bind())
