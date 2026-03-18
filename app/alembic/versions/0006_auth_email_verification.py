"""Add pending email verification state to app auth settings.

Revision ID: 0006_auth_email_verification
Revises: 0005_auth_foundation
Create Date: 2026-03-09 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006_auth_email_verification"
down_revision = "0005_auth_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_auth_settings",
        sa.Column("pending_email", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "app_auth_settings",
        sa.Column("email_verification_code_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "app_auth_settings",
        sa.Column(
            "email_verification_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "app_auth_settings",
        sa.Column(
            "email_verification_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("app_auth_settings", "email_verification_attempt_count")
    op.drop_column("app_auth_settings", "email_verification_expires_at")
    op.drop_column("app_auth_settings", "email_verification_code_hash")
    op.drop_column("app_auth_settings", "pending_email")
