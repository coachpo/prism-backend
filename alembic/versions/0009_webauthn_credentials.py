"""Add webauthn_credentials table

Revision ID: 0009_webauthn_credentials
Revises: 0008_loadbalance_events
Create Date: 2026-03-12

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0009_webauthn_credentials"
down_revision = "0008_loadbalance_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webauthn_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("auth_subject_id", sa.Integer(), nullable=False),
        # WebAuthn core fields
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False, server_default="0"),
        # Device management
        sa.Column("device_name", sa.String(length=200), nullable=True),
        sa.Column("aaguid", sa.LargeBinary(), nullable=True),
        sa.Column("transports", sa.ARRAY(sa.Text()), nullable=True),
        # Backup and sync identifiers
        sa.Column(
            "backup_eligible", sa.Boolean(), nullable=True, server_default="false"
        ),
        sa.Column("backup_state", sa.Boolean(), nullable=True, server_default="false"),
        # Audit fields
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=45), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["auth_subject_id"],
            ["app_auth_settings.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("credential_id", name="uq_credential_id"),
    )
    op.create_index(
        "idx_webauthn_credentials_auth_subject",
        "webauthn_credentials",
        ["auth_subject_id"],
    )
    op.create_index(
        "idx_webauthn_credentials_last_used",
        "webauthn_credentials",
        ["last_used_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_webauthn_credentials_last_used", table_name="webauthn_credentials"
    )
    op.drop_index(
        "idx_webauthn_credentials_auth_subject", table_name="webauthn_credentials"
    )
    op.drop_table("webauthn_credentials")
