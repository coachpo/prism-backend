"""Backfill default profile and scoped profile_id references

Revision ID: 0003_profiles_backfill
Revises: 0002_profiles_additive
Create Date: 2026-02-28 18:10:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0003_profiles_backfill"
down_revision = "0002_profiles_additive"
branch_labels = None
depends_on = None

DEFAULT_PROFILE_NAME = "Default"
DEFAULT_PROFILE_DESCRIPTION = "Default profile migrated from global namespace"


def upgrade() -> None:
    bind = op.get_bind()

    bind.execute(
        sa.text(
            """
            INSERT INTO profiles (name, description, is_active, version, deleted_at, created_at, updated_at)
            SELECT CAST(:name AS VARCHAR), CAST(:description AS TEXT), true, 0, NULL, NOW(), NOW()
            WHERE NOT EXISTS (SELECT 1 FROM profiles WHERE name = CAST(:name AS VARCHAR))
            """
        ),
        {"name": DEFAULT_PROFILE_NAME, "description": DEFAULT_PROFILE_DESCRIPTION},
    )

    default_profile_id = bind.execute(
        sa.text("SELECT id FROM profiles WHERE name = :name LIMIT 1"),
        {"name": DEFAULT_PROFILE_NAME},
    ).scalar_one()

    active_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM profiles WHERE is_active = true")
    ).scalar_one()
    if int(active_count or 0) == 0:
        bind.execute(
            sa.text(
                "UPDATE profiles SET is_active = true, updated_at = NOW() WHERE id = :profile_id"
            ),
            {"profile_id": default_profile_id},
        )

    for table_name in (
        "model_configs",
        "endpoints",
        "connections",
        "request_logs",
        "audit_logs",
        "endpoint_fx_rate_settings",
    ):
        bind.execute(
            sa.text(
                f"UPDATE {table_name} SET profile_id = :profile_id WHERE profile_id IS NULL"
            ),
            {"profile_id": default_profile_id},
        )

    bind.execute(
        sa.text(
            """
            UPDATE header_blocklist_rules
            SET profile_id = :profile_id
            WHERE profile_id IS NULL AND is_system = false
            """
        ),
        {"profile_id": default_profile_id},
    )

    bind.execute(
        sa.text(
            """
            UPDATE user_settings
            SET profile_id = :profile_id
            WHERE profile_id IS NULL
            """
        ),
        {"profile_id": default_profile_id},
    )

    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (PARTITION BY profile_id ORDER BY id ASC) AS row_num
                FROM user_settings
                WHERE profile_id IS NOT NULL
            )
            DELETE FROM user_settings us
            USING ranked r
            WHERE us.id = r.id AND r.row_num > 1
            """
        )
    )

    bind.execute(
        sa.text(
            """
            INSERT INTO user_settings (
                profile_id,
                report_currency_code,
                report_currency_symbol,
                timezone_preference,
                created_at,
                updated_at
            )
            SELECT :profile_id, 'USD', '$', NULL, NOW(), NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM user_settings WHERE profile_id = :profile_id
            )
            """
        ),
        {"profile_id": default_profile_id},
    )

    bind.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY profile_id, model_id, endpoint_id
                           ORDER BY id ASC
                       ) AS row_num
                FROM endpoint_fx_rate_settings
                WHERE profile_id IS NOT NULL
            )
            DELETE FROM endpoint_fx_rate_settings fx
            USING ranked r
            WHERE fx.id = r.id AND r.row_num > 1
            """
        )
    )


def downgrade() -> None:
    pass
