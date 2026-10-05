"""Download the wallets' history once more, now including Solana transaction v1.

Until this version every getTransaction asked for ``maxSupportedTransactionVersion: 0``
and the RPC rejected each v1 transaction (the format live on mainnet since
2026-09-15), so wallets ended up with a large part of their recent trades missing.
The re-scan only downloads what is not already stored.

Revision ID: 0005_rescan_for_tx_v1
Revises: 0004_rescan_wallet_history
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_rescan_for_tx_v1"
down_revision: str | None = "0004_rescan_wallet_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE wallets SET backfilled_at = NULL")


def downgrade() -> None:
    pass  # nothing to undo: the next backfill marks them again
