"""card cache

Revision ID: 0006_card_cache
Revises: 0005_battles
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_card_cache"
down_revision = "0005_battles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "card",
        sa.Column("match_id", sa.Text(), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=False),
        sa.Column("numerator", sa.Text(), nullable=False),
        sa.Column("set_name", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("rarity", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("source", sa.Text(), server_default="lookup", nullable=False),
        sa.Column("first_seen", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_fetched", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("match_id", name="pk_card"),
    )
    # Non-unique: one (set, number) can map to several match_ids (variants/reprints).
    op.create_index("ix_card_set_id_numerator", "card", ["set_id", "numerator"])


def downgrade() -> None:
    op.drop_table("card")
