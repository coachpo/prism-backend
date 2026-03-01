"""Remove deprecated forward_stream_options column

Revision ID: 0005_drop_forward_stream_options
Revises: 0004_profiles_constraints
Create Date: 2026-03-01 03:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0005_drop_forward_stream_options"
down_revision = "0004_profiles_constraints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("connections", "forward_stream_options")


def downgrade() -> None:
    op.add_column(
        "connections",
        sa.Column(
            "forward_stream_options",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.alter_column("connections", "forward_stream_options", server_default=None)
