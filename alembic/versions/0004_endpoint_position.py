"""Add persisted endpoint ordering positions.

Revision ID: 0004_endpoint_position
Revises: 0003_pricing_templates_v2
Create Date: 2026-03-07 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0004_endpoint_position"
down_revision = "0003_pricing_templates_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("endpoints", sa.Column("position", sa.Integer(), nullable=True))

    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY profile_id
                        ORDER BY id ASC
                    ) - 1 AS normalized_position
                FROM endpoints
            )
            UPDATE endpoints AS endpoints_to_update
            SET position = ranked.normalized_position
            FROM ranked
            WHERE endpoints_to_update.id = ranked.id
            """
        )
    )

    op.alter_column("endpoints", "position", nullable=False)
    op.create_index(
        "idx_endpoints_profile_position",
        "endpoints",
        ["profile_id", "position"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_endpoints_profile_position", table_name="endpoints")
    op.drop_column("endpoints", "position")
