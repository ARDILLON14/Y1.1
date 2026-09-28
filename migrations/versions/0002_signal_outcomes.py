"""Signal outcomes: forward returns of executed and rejected COPY signals.

Revision ID: 0002_signal_outcomes
Revises: 0001_initial
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from copytrader.db.base import JSONType, UTCDateTime

revision: str = "0002_signal_outcomes"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signal_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("wallet_id", sa.Integer(), sa.ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_mint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(8), nullable=True),
        sa.Column("failed_check", sa.String(40), nullable=True),
        sa.Column("failed_label", sa.String(120), nullable=True),
        sa.Column("reference_price_usd", sa.Float(), nullable=False),
        sa.Column("reference_at", UTCDateTime(), nullable=False),
        sa.Column("returns", JSONType, nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("signal_id"),
    )
    op.create_index("ix_signal_outcomes_wallet_id", "signal_outcomes", ["wallet_id"])
    op.create_index("ix_signal_outcomes_status", "signal_outcomes", ["status"])
    op.create_index("ix_signal_outcomes_failed_check", "signal_outcomes", ["failed_check"])
    op.create_index("ix_signal_outcomes_reference_at", "signal_outcomes", ["reference_at"])
    op.create_index("ix_signal_outcomes_completed", "signal_outcomes", ["completed"])


def downgrade() -> None:
    op.drop_table("signal_outcomes")
