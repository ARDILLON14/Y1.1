"""Download the wallets' history once more.

Earlier versions marked a wallet as backfilled even when part of its
transactions could not be downloaded (RPC throttling opened the circuit
breaker and every pending download failed at once), so its metrics could be
computed on a fraction of its history. The re-scan only downloads what is not
already stored.

Revision ID: 0004_rescan_wallet_history
Revises: 0003_price_history
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_rescan_wallet_history"
down_revision: str | None = "0003_price_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE wallets SET backfilled_at = NULL")


def downgrade() -> None:
    pass  # nothing to undo: the next backfill marks them again
