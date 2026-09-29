"""Historical price candles (and their download log) for backtesting with real prices.

Revision ID: 0003_price_history
Revises: 0002_signal_outcomes
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from copytrader.db.base import UTCDateTime

revision: str = "0003_price_history"
down_revision: str | None = "0002_signal_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "price_candles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mint", sa.String(64), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("ts", UTCDateTime(), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume_usd", sa.Float(), nullable=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.UniqueConstraint("mint", "interval_minutes", "ts", name="uq_price_candle"),
    )
    op.create_index("ix_price_candles_mint_ts", "price_candles", ["mint", "interval_minutes", "ts"])
    op.create_table(
        "price_fetches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mint", sa.String(64), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("start", UTCDateTime(), nullable=False),
        sa.Column("end", UTCDateTime(), nullable=False),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("pool", sa.String(64), nullable=True),
        sa.Column("candles", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_price_fetches_mint", "price_fetches", ["mint", "interval_minutes"])


def downgrade() -> None:
    op.drop_table("price_fetches")
    op.drop_table("price_candles")
