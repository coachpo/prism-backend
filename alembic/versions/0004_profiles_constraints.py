"""Enforce profile constraints and scoped uniqueness

Revision ID: 0004_profiles_constraints
Revises: 0003_profiles_backfill
Create Date: 2026-02-28 18:18:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0004_profiles_constraints"
down_revision = "0003_profiles_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("profiles", "is_active", server_default=None)
    op.alter_column("profiles", "version", server_default=None)

    op.alter_column("model_configs", "profile_id", nullable=False)
    op.alter_column("endpoints", "profile_id", nullable=False)
    op.alter_column("connections", "profile_id", nullable=False)
    op.alter_column("user_settings", "profile_id", nullable=False)
    op.alter_column("endpoint_fx_rate_settings", "profile_id", nullable=False)
    op.alter_column("request_logs", "profile_id", nullable=False)
    op.alter_column("audit_logs", "profile_id", nullable=False)

    op.create_unique_constraint(
        "uq_model_configs_profile_model_id",
        "model_configs",
        ["profile_id", "model_id"],
    )
    op.drop_constraint("model_configs_model_id_key", "model_configs", type_="unique")

    op.create_unique_constraint(
        "uq_endpoints_profile_name",
        "endpoints",
        ["profile_id", "name"],
    )
    op.drop_constraint("endpoints_name_key", "endpoints", type_="unique")

    op.create_unique_constraint(
        "uq_fx_profile_model_endpoint",
        "endpoint_fx_rate_settings",
        ["profile_id", "model_id", "endpoint_id"],
    )
    op.drop_constraint(
        "uq_fx_model_endpoint",
        "endpoint_fx_rate_settings",
        type_="unique",
    )

    op.create_unique_constraint(
        "uq_user_settings_profile_id",
        "user_settings",
        ["profile_id"],
    )

    op.create_unique_constraint(
        "uq_hbr_profile_match_pattern",
        "header_blocklist_rules",
        ["profile_id", "match_type", "pattern"],
    )
    op.drop_constraint(
        "uq_match_type_pattern",
        "header_blocklist_rules",
        type_="unique",
    )

    op.create_check_constraint(
        "ck_hbr_profile_scope",
        "header_blocklist_rules",
        "((is_system = true AND profile_id IS NULL) OR (is_system = false AND profile_id IS NOT NULL))",
    )
    op.create_index(
        "uq_hbr_system_match_pattern",
        "header_blocklist_rules",
        ["match_type", "pattern"],
        unique=True,
        postgresql_where=sa.text("is_system = true"),
    )

    op.create_index(
        "idx_model_configs_profile_model_enabled",
        "model_configs",
        ["profile_id", "model_id", "is_enabled"],
        unique=False,
    )
    op.create_index(
        "idx_connections_profile_model_active_priority",
        "connections",
        ["profile_id", "model_config_id", "is_active", "priority"],
        unique=False,
    )
    op.create_index(
        "idx_fx_profile_model_endpoint",
        "endpoint_fx_rate_settings",
        ["profile_id", "model_id", "endpoint_id"],
        unique=False,
    )
    op.create_index(
        "idx_request_logs_profile_created_at",
        "request_logs",
        ["profile_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_audit_logs_profile_created_at",
        "audit_logs",
        ["profile_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_audit_logs_profile_created_at", table_name="audit_logs")
    op.drop_index("idx_request_logs_profile_created_at", table_name="request_logs")
    op.drop_index(
        "idx_fx_profile_model_endpoint", table_name="endpoint_fx_rate_settings"
    )
    op.drop_index(
        "idx_connections_profile_model_active_priority", table_name="connections"
    )
    op.drop_index("idx_model_configs_profile_model_enabled", table_name="model_configs")

    op.drop_index("uq_hbr_system_match_pattern", table_name="header_blocklist_rules")
    op.drop_constraint(
        "ck_hbr_profile_scope",
        "header_blocklist_rules",
        type_="check",
    )
    op.drop_constraint(
        "uq_hbr_profile_match_pattern",
        "header_blocklist_rules",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_match_type_pattern",
        "header_blocklist_rules",
        ["match_type", "pattern"],
    )

    op.drop_constraint(
        "uq_user_settings_profile_id",
        "user_settings",
        type_="unique",
    )

    op.drop_constraint(
        "uq_fx_profile_model_endpoint",
        "endpoint_fx_rate_settings",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_fx_model_endpoint",
        "endpoint_fx_rate_settings",
        ["model_id", "endpoint_id"],
    )

    op.drop_constraint("uq_endpoints_profile_name", "endpoints", type_="unique")
    op.create_unique_constraint("endpoints_name_key", "endpoints", ["name"])

    op.drop_constraint(
        "uq_model_configs_profile_model_id",
        "model_configs",
        type_="unique",
    )
    op.create_unique_constraint(
        "model_configs_model_id_key", "model_configs", ["model_id"]
    )

    op.alter_column("audit_logs", "profile_id", nullable=True)
    op.alter_column("request_logs", "profile_id", nullable=True)
    op.alter_column("endpoint_fx_rate_settings", "profile_id", nullable=True)
    op.alter_column("user_settings", "profile_id", nullable=True)
    op.alter_column("connections", "profile_id", nullable=True)
    op.alter_column("endpoints", "profile_id", nullable=True)
    op.alter_column("model_configs", "profile_id", nullable=True)

    op.alter_column("profiles", "version", server_default=sa.text("0"))
    op.alter_column("profiles", "is_active", server_default=sa.text("false"))
