from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_connection_probe"
down_revision = "0002_monitoring_hot_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column(
            "monitoring_probe_interval_seconds",
            sa.Integer(),
            nullable=True,
            server_default="300",
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE connections
            SET monitoring_probe_interval_seconds = 300
            WHERE monitoring_probe_interval_seconds IS NULL
            """
        )
    )
    op.alter_column(
        "connections",
        "monitoring_probe_interval_seconds",
        nullable=False,
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("connections", "monitoring_probe_interval_seconds")
