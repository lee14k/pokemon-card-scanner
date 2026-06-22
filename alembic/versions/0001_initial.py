"""initial schema: trainer, pull, pull_card + citext + partial unique index

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "trainer",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("handle", CITEXT(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_trainer"),
        sa.UniqueConstraint("handle", name="uq_trainer_handle"),
    )
    op.create_index("ix_trainer_email", "trainer", ["email"], unique=True)

    op.create_table(
        "pull",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trainer_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("capture_path", sa.String(length=16), nullable=False),
        sa.Column("pack_confidence", sa.Float(), nullable=False),
        sa.Column("segmentation_warning", sa.Text(), nullable=True),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("code_normalized", sa.Text(), nullable=True),
        sa.Column("code_confidence", sa.Float(), nullable=False),
        sa.Column("code_format_ok", sa.Boolean(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("staircase_photo_path", sa.Text(), nullable=False),
        sa.Column("code_photo_path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["trainer_id"], ["trainer.id"], name="fk_pull_trainer_id_trainer", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_pull"),
    )
    op.create_index("ix_pull_trainer_id", "pull", ["trainer_id"])
    op.create_index("ix_pull_verified", "pull", ["verified"])
    # The anti-fraud invariant: at most one VERIFIED pull per normalized code.
    op.create_index(
        "uq_pull_verified_code",
        "pull",
        ["code_normalized"],
        unique=True,
        postgresql_where=sa.text("verified = true AND code_normalized IS NOT NULL"),
    )

    op.create_table(
        "pull_card",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pull_id", sa.Uuid(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("card_number", sa.Text(), nullable=True),
        sa.Column("set_id", sa.Text(), nullable=True),
        sa.Column("set_code", sa.Text(), nullable=True),
        sa.Column("set_name", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("rarity", sa.Text(), nullable=True),
        sa.Column("low_confidence_reason", sa.Text(), nullable=True),
        sa.Column("match_id", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["pull_id"], ["pull.id"], name="fk_pull_card_pull_id_pull", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_pull_card"),
    )
    op.create_index("ix_pull_card_pull_id", "pull_card", ["pull_id"])


def downgrade() -> None:
    op.drop_table("pull_card")
    op.drop_index("uq_pull_verified_code", table_name="pull")
    op.drop_index("ix_pull_verified", table_name="pull")
    op.drop_index("ix_pull_trainer_id", table_name="pull")
    op.drop_table("pull")
    op.drop_index("ix_trainer_email", table_name="trainer")
    op.drop_table("trainer")
    op.execute("DROP EXTENSION IF EXISTS citext")
