"""pull-rate stats: role, derived cards, stat + anomaly tables

Revision ID: 0002_pull_rate_stats
Revises: 0001_initial
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_pull_rate_stats"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trainer", sa.Column("role", sa.String(length=16), nullable=False, server_default="trainer"))
    op.add_column("pull", sa.Column("capture_meta", JSONB(), nullable=True))
    op.add_column("pull", sa.Column("derive_status", sa.String(length=16), nullable=False, server_default="pending"))
    op.add_column("pull", sa.Column("derived_at", sa.TIMESTAMP(timezone=True), nullable=True))

    op.create_table(
        "pull_card_derived",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("pull_id", sa.Uuid(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("card_number", sa.Text(), nullable=True),
        sa.Column("set_id", sa.Text(), nullable=True),
        sa.Column("set_code", sa.Text(), nullable=True),
        sa.Column("set_name", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("rarity", sa.Text(), nullable=True),
        sa.Column("match_id", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["pull_id"], ["pull.id"], name="fk_pull_card_derived_pull_id_pull", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_pull_card_derived"),
    )
    op.create_index("ix_pull_card_derived_pull_id", "pull_card_derived", ["pull_id"])
    op.create_index("ix_pull_card_derived_set_id", "pull_card_derived", ["set_id"])
    op.create_index("ix_pull_card_derived_match_id", "pull_card_derived", ["match_id"])

    op.create_table(
        "stats_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_stats_snapshot"),
    )

    op.create_table(
        "set_stat",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=False),
        sa.Column("verified_pack_count", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["stats_snapshot.id"], name="fk_set_stat_snapshot_id_stats_snapshot", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_set_stat"),
    )
    op.create_index("ix_set_stat_snapshot_id", "set_stat", ["snapshot_id"])
    op.create_index("ix_set_stat_set_id", "set_stat", ["set_id"])

    op.create_table(
        "rarity_stat",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=False),
        sa.Column("rarity", sa.Text(), nullable=False),
        sa.Column("packs_with_rarity", sa.Integer(), nullable=False),
        sa.Column("raw_rate", sa.Float(), nullable=False),
        sa.Column("blended_rate", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["stats_snapshot.id"], name="fk_rarity_stat_snapshot_id_stats_snapshot", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_rarity_stat"),
    )
    op.create_index("ix_rarity_stat_snapshot_id", "rarity_stat", ["snapshot_id"])

    op.create_table(
        "card_stat",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=False),
        sa.Column("match_id", sa.Text(), nullable=False),
        sa.Column("card_number", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("packs", sa.Integer(), nullable=False),
        sa.Column("raw_rate", sa.Float(), nullable=False),
        sa.Column("blended_rate", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["stats_snapshot.id"], name="fk_card_stat_snapshot_id_stats_snapshot", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_card_stat"),
    )
    op.create_index("ix_card_stat_snapshot_id", "card_stat", ["snapshot_id"])
    op.create_index("ix_card_stat_set_id", "card_stat", ["set_id"])

    op.create_table(
        "anomaly",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("detector", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=8), nullable=False),
        sa.Column("set_id", sa.Text(), nullable=False),
        sa.Column("card_match_id", sa.Text(), nullable=True),
        sa.Column("severity", sa.Float(), nullable=False),
        sa.Column("detail", JSONB(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["stats_snapshot.id"], name="fk_anomaly_snapshot_id_stats_snapshot", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_anomaly"),
    )
    op.create_index("ix_anomaly_snapshot_id", "anomaly", ["snapshot_id"])


def downgrade() -> None:
    op.drop_table("anomaly")
    op.drop_table("card_stat")
    op.drop_table("rarity_stat")
    op.drop_table("set_stat")
    op.drop_table("stats_snapshot")
    op.drop_table("pull_card_derived")
    op.drop_column("pull", "derived_at")
    op.drop_column("pull", "derive_status")
    op.drop_column("pull", "capture_meta")
    op.drop_column("trainer", "role")
