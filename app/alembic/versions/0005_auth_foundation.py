"""Add application auth and proxy key foundation.

Revision ID: 0005_auth_foundation
Revises: 0005_conn_priority_norm
Create Date: 2026-03-09 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005_auth_foundation"
down_revision = "0005_conn_priority_norm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_auth_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("singleton_key", sa.String(length=20), nullable=False),
        sa.Column(
            "auth_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("username", sa.String(length=200), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("email_bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("singleton_key", name="uq_app_auth_settings_singleton_key"),
    )

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("auth_subject_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_from_id", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["auth_subject_id"], ["app_auth_settings.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["rotated_from_id"], ["refresh_tokens.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("idx_refresh_tokens_revoked_at", "refresh_tokens", ["revoked_at"])
    op.create_index("idx_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"])

    op.create_table(
        "proxy_api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("key_prefix", sa.String(length=200), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("last_four", sa.String(length=4), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=100), nullable=True),
        sa.Column("created_by_auth_subject_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("rotated_from_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_auth_subject_id"],
            ["app_auth_settings.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rotated_from_id"], ["proxy_api_keys.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("key_prefix", name="uq_proxy_api_keys_prefix"),
    )
    op.create_index("idx_proxy_api_keys_is_active", "proxy_api_keys", ["is_active"])

    op.create_table(
        "password_reset_challenges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("auth_subject_id", sa.Integer(), nullable=False),
        sa.Column("otp_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requested_ip", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["auth_subject_id"], ["app_auth_settings.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "idx_password_reset_challenges_expires_at",
        "password_reset_challenges",
        ["expires_at"],
    )
    op.create_index(
        "idx_password_reset_challenges_consumed_at",
        "password_reset_challenges",
        ["consumed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_password_reset_challenges_consumed_at",
        table_name="password_reset_challenges",
    )
    op.drop_index(
        "idx_password_reset_challenges_expires_at",
        table_name="password_reset_challenges",
    )
    op.drop_table("password_reset_challenges")

    op.drop_index("idx_proxy_api_keys_is_active", table_name="proxy_api_keys")
    op.drop_table("proxy_api_keys")

    op.drop_index("idx_refresh_tokens_expires_at", table_name="refresh_tokens")
    op.drop_index("idx_refresh_tokens_revoked_at", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")

    op.drop_table("app_auth_settings")
