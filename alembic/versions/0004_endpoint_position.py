"""Add persisted endpoint ordering positions.

Compatibility migration for databases created before the squashed baseline.
Fresh databases already have the target schema in ``0001_initial``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0004_endpoint_position"
down_revision = "0003_pricing_templates_v2"
branch_labels = None
depends_on = None


def _get_position_column() -> dict[str, object] | None:
    inspector = sa.inspect(op.get_bind())
    for column in inspector.get_columns("endpoints"):
        if column["name"] == "position":
            return column
    return None


def _index_exists(index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        index["name"] == index_name for index in inspector.get_indexes("endpoints")
    )


def _normalize_positions() -> None:
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


def upgrade() -> None:
    position_column = _get_position_column()
    if position_column is None:
        op.add_column("endpoints", sa.Column("position", sa.Integer(), nullable=True))
        position_column = _get_position_column()

    if position_column is None or bool(position_column.get("nullable", True)):
        _normalize_positions()
        op.alter_column("endpoints", "position", nullable=False)

    if not _index_exists("idx_endpoints_profile_position"):
        op.create_index(
            "idx_endpoints_profile_position",
            "endpoints",
            ["profile_id", "position"],
            unique=False,
        )


def downgrade() -> None:
    pass
