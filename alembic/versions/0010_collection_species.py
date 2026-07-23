"""collection_card.species for the Pokédex

Revision ID: 0010_collection_species
Revises: 0009_collection
Create Date: 2026-07-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.dex.species import species_of

revision = "0010_collection_species"
down_revision = "0009_collection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collection_card", sa.Column("species", sa.Text(), nullable=True))
    op.create_index("ix_collection_card_species", "collection_card", ["species"])
    # 0003 added pull_card.species to an empty table (no backfill needed); collection
    # already has dev rows, so derive species from name in Python for the existing set.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, name FROM collection_card WHERE name IS NOT NULL")
    ).fetchall()
    for row_id, name in rows:
        sp = species_of(name)
        if sp is not None:
            bind.execute(
                sa.text("UPDATE collection_card SET species = :sp WHERE id = :id"),
                {"sp": sp, "id": row_id},
            )


def downgrade() -> None:
    op.drop_index("ix_collection_card_species", table_name="collection_card")
    op.drop_column("collection_card", "species")
