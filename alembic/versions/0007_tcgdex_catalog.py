"""tcgdex catalog + id maps

Revision ID: 0007_tcgdex_catalog
Revises: 0006_card_cache
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_tcgdex_catalog"
down_revision = "0006_card_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tcgdex_set",
        sa.Column("id", sa.Text(), nullable=False),  # e.g. "sv06"
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("series", sa.Text(), nullable=False),  # "swsh" | "sv"
        sa.Column("card_count_official", sa.Integer(), nullable=True),
        sa.Column("card_count_total", sa.Integer(), nullable=True),
        sa.Column("raw", JSONB(), nullable=False),
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tcgdex_set"),
    )

    op.create_table(
        "tcgdex_card",
        sa.Column("id", sa.Text(), nullable=False),  # e.g. "sv06-101"
        sa.Column("set_id", sa.Text(), nullable=False),
        sa.Column("local_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("image_base", sa.Text(), nullable=True),
        sa.Column("raw", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tcgdex_card"),
    )
    op.create_index("ix_tcgdex_card_set_id", "tcgdex_card", ["set_id"])
    op.create_index("ix_tcgdex_card_set_id_local_id", "tcgdex_card", ["set_id", "local_id"])

    op.create_table(
        "set_id_map",
        sa.Column("pokewallet_set_id", sa.Text(), nullable=False),
        sa.Column("tcgdex_set_id", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),  # name | name+count
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("built_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("pokewallet_set_id", name="pk_set_id_map"),
    )

    op.create_table(
        "card_id_map",
        sa.Column("pokewallet_match_id", sa.Text(), nullable=False),
        sa.Column("tcgdex_card_id", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),  # set+number
        sa.Column("built_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("pokewallet_match_id", name="pk_card_id_map"),
    )


def downgrade() -> None:
    op.drop_table("card_id_map")
    op.drop_table("set_id_map")
    op.drop_table("tcgdex_card")
    op.drop_table("tcgdex_set")
