"""training data pools

Revision ID: 0008_training_data
Revises: 0007_tcgdex_catalog
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_training_data"
down_revision = "0007_tcgdex_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "training_photo",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), server_default="standard", nullable=False),  # standard|stress
        sa.Column("split", sa.Text(), server_default="train", nullable=False),  # train|test
        sa.Column("labeled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("set_hint", sa.Text(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["trainer.id"],
            name="fk_training_photo_uploaded_by_trainer", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_training_photo"),
    )

    op.create_table(
        "training_strip",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("photo_id", sa.Uuid(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=True),
        sa.Column("card_key", sa.Text(), nullable=True),  # catalog id, e.g. "sv06-45"
        sa.Column("source", sa.Text(), server_default="upload", nullable=False),  # upload|harvest
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["photo_id"], ["training_photo.id"],
            name="fk_training_strip_photo_id_training_photo", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_training_strip"),
    )
    op.create_index("ix_training_strip_photo_id", "training_strip", ["photo_id"])

    op.create_table(
        "eval_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("top1", sa.Integer(), nullable=True),
        sa.Column("top3", sa.Integer(), nullable=True),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("detail", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_eval_run"),
    )


def downgrade() -> None:
    op.drop_table("eval_run")
    op.drop_table("training_strip")
    op.drop_table("training_photo")
