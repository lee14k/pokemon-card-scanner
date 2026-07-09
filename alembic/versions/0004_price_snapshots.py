"""price snapshots: price_snapshot + card_price

Revision ID: 0004_price_snapshots
Revises: 0003_pull_card_species
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_price_snapshots"
down_revision = "0003_pull_card_species"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_price_snapshot"),
    )
    op.create_table(
        "card_price",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("match_id", sa.Text(), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=True),
        sa.Column("card_number", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("usd_market_low", sa.Float(), nullable=True),
        sa.Column("usd_market_high", sa.Float(), nullable=True),
        sa.Column("eur_trend", sa.Float(), nullable=True),
        sa.Column("raw", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["price_snapshot.id"], name="fk_card_price_snapshot_id_price_snapshot", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_card_price"),
    )
    op.create_index("ix_card_price_snapshot_id", "card_price", ["snapshot_id"])
    op.create_index("ix_card_price_match_id", "card_price", ["match_id"])


def downgrade() -> None:
    op.drop_table("card_price")
    op.drop_table("price_snapshot")
