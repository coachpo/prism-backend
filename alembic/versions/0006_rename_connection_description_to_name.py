"""Rename connections.description to connections.name

Revision ID: 0006_conn_name_col
Revises: 0005_drop_forward_stream_options
Create Date: 2026-03-02 12:00:00
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "0006_conn_name_col"
down_revision = "0005_drop_forward_stream_options"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("connections", "description", new_column_name="name")


def downgrade() -> None:
    op.alter_column("connections", "name", new_column_name="description")
