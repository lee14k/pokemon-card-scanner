"""pack battles

Revision ID: 0005_battles
Revises: 0004_price_snapshots
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_battles"
down_revision = "0004_price_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "battle",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("challenger_id", sa.Uuid(), nullable=False),
        sa.Column("challenger_pull_id", sa.Uuid(), nullable=False),
        sa.Column("opponent_id", sa.Uuid(), nullable=True),
        sa.Column("opponent_pull_id", sa.Uuid(), nullable=True),
        sa.Column("bot_pack", JSONB(), nullable=True),
        sa.Column("challenger_score", sa.Float(), nullable=True),
        sa.Column("opponent_score", sa.Float(), nullable=True),
        sa.Column("winner", sa.String(length=12), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["challenger_id"], ["trainer.id"], name="fk_battle_challenger_id_trainer", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["challenger_pull_id"], ["pull.id"], name="fk_battle_challenger_pull_id_pull", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opponent_id"], ["trainer.id"], name="fk_battle_opponent_id_trainer", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opponent_pull_id"], ["pull.id"], name="fk_battle_opponent_pull_id_pull", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_battle"),
    )
    op.create_index("ix_battle_challenger_id", "battle", ["challenger_id"])
    op.create_index("ix_battle_opponent_id", "battle", ["opponent_id"])


def downgrade() -> None:
    op.drop_table("battle")
