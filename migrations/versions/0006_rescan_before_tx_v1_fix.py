"""Re-scan the wallets' history once more, reaching further back if the limit was raised.

Until the transaction v1 fix (0005) the live feed also missed every v1 transaction.
A wallet that buys with v1 and sells with v0 was left with sales whose purchase was
never stored ("ventas sin compra") for everything recorded before the fix, and the
0005 re-scan only reached its last ``backfill_max_signatures_per_wallet``
transactions (about two weeks for an active wallet with the default 1,000). Raise
that limit before applying this migration to repair older periods; either way only
the transactions not stored yet are downloaded.

Revision ID: 0006_rescan_before_tx_v1_fix
Revises: 0005_rescan_for_tx_v1
Create Date: 2026-10-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_rescan_before_tx_v1_fix"
down_revision: str | None = "0005_rescan_for_tx_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE wallets SET backfilled_at = NULL")


def downgrade() -> None:
    pass  # nothing to undo: the next backfill marks them again
