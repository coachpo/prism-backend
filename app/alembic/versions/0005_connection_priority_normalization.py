"""Normalize connection priorities per profile and model.

Revision ID: 0005_conn_priority_norm
Revises: 0004_endpoint_position
Create Date: 2026-03-07 00:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005_conn_priority_norm"
down_revision = "0004_endpoint_position"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY profile_id, model_config_id
                        ORDER BY priority ASC, id ASC
                    ) - 1 AS normalized_priority
                FROM connections
            )
            UPDATE connections AS connections_to_update
            SET priority = ranked.normalized_priority
            FROM ranked
            WHERE connections_to_update.id = ranked.id
              AND connections_to_update.priority IS DISTINCT FROM ranked.normalized_priority
            """
        )
    )


def downgrade() -> None:
    pass
