"""pull_card.species for the Pokédex

Revision ID: 0003_pull_card_species
Revises: 0002_pull_rate_stats
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_pull_card_species"
down_revision = "0002_pull_rate_stats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pull_card", sa.Column("species", sa.Text(), nullable=True))
    op.create_index("ix_pull_card_species", "pull_card", ["species"])


def downgrade() -> None:
    op.drop_index("ix_pull_card_species", table_name="pull_card")
    op.drop_column("pull_card", "species")
