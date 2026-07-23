"""collection cards

Revision ID: 0009_collection
Revises: 0008_training_data
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_collection"
down_revision = "0008_training_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_card",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trainer_id", sa.Uuid(), nullable=False),
        sa.Column("tcgdex_card_id", sa.Text(), nullable=True),
        sa.Column("set_id", sa.Text(), nullable=True),
        sa.Column("set_code", sa.Text(), nullable=True),
        sa.Column("set_name", sa.Text(), nullable=True),
        sa.Column("card_number", sa.Text(), nullable=True),
        sa.Column("numerator", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("match_id", sa.Text(), nullable=True),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("qty", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["trainer_id"], ["trainer.id"],
            name="fk_collection_card_trainer_id_trainer", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_collection_card"),
        sa.UniqueConstraint("trainer_id", "identity_key", name="uq_collection_trainer_identity"),
    )
    op.create_index("ix_collection_card_trainer_id", "collection_card", ["trainer_id"])


def downgrade() -> None:
    op.drop_table("collection_card")
