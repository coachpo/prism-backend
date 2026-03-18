"""Add webauthn_challenges table for persistent challenge storage

Revision ID: 0010_webauthn_challenges
Revises: 0009_webauthn_credentials
Create Date: 2026-03-13

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0010_webauthn_challenges"
down_revision = "0009_webauthn_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create UNLOGGED table for better performance (data not written to WAL)
    # Note: UNLOGGED tables are faster but data is lost on crash
    # This is acceptable for temporary challenge storage with 2-minute TTL
    op.execute("""
        CREATE UNLOGGED TABLE webauthn_challenges (
            id SERIAL PRIMARY KEY,
            challenge_key VARCHAR(100) NOT NULL UNIQUE,
            challenge BYTEA NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
        )
    """)
    op.create_index(
        "idx_webauthn_challenges_expires_at",
        "webauthn_challenges",
        ["expires_at"],
    )
    op.create_index(
        "idx_webauthn_challenges_challenge_key",
        "webauthn_challenges",
        ["challenge_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_webauthn_challenges_challenge_key", table_name="webauthn_challenges"
    )
    op.drop_index(
        "idx_webauthn_challenges_expires_at", table_name="webauthn_challenges"
    )
    op.drop_table("webauthn_challenges")
