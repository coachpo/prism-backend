from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0007_refresh_session_duration"
down_revision = "0006_auth_email_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column(
            "session_duration",
            sa.String(length=20),
            nullable=False,
            server_default="7_days",
        ),
    )


def downgrade() -> None:
    op.drop_column("refresh_tokens", "session_duration")
