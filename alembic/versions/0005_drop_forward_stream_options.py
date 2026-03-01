"""Squashed baseline schema

Revision ID: 0005_drop_forward_stream_options
Revises:
Create Date: 2026-03-01 03:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0005_drop_forward_stream_options"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("idx_profiles_deleted_at", "profiles", ["deleted_at"], unique=False)
    op.create_index(
        "uq_profiles_single_active",
        "profiles",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    op.create_table(
        "providers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("provider_type", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("audit_enabled", sa.Boolean(), nullable=False),
        sa.Column("audit_capture_bodies", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "model_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("model_type", sa.String(length=20), nullable=False),
        sa.Column("redirect_to", sa.String(length=200), nullable=True),
        sa.Column("lb_strategy", sa.String(length=50), nullable=False),
        sa.Column("failover_recovery_enabled", sa.Boolean(), nullable=False),
        sa.Column("failover_recovery_cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "model_id", name="uq_model_configs_profile_model_id"
        ),
    )
    op.create_index(
        "ix_model_configs_profile_id", "model_configs", ["profile_id"], unique=False
    )
    op.create_index(
        "idx_model_configs_profile_model_enabled",
        "model_configs",
        ["profile_id", "model_id", "is_enabled"],
        unique=False,
    )

    op.create_table(
        "endpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("api_key", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", "name", name="uq_endpoints_profile_name"),
    )
    op.create_index(
        "ix_endpoints_profile_id", "endpoints", ["profile_id"], unique=False
    )

    op.create_table(
        "connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("auth_type", sa.String(length=50), nullable=True),
        sa.Column("custom_headers", sa.Text(), nullable=True),
        sa.Column("health_status", sa.String(length=20), nullable=False),
        sa.Column("health_detail", sa.Text(), nullable=True),
        sa.Column("last_health_check", sa.DateTime(), nullable=True),
        sa.Column("pricing_enabled", sa.Boolean(), nullable=False),
        sa.Column("pricing_currency_code", sa.String(length=3), nullable=True),
        sa.Column("input_price", sa.String(length=20), nullable=True),
        sa.Column("output_price", sa.String(length=20), nullable=True),
        sa.Column("cached_input_price", sa.String(length=20), nullable=True),
        sa.Column("cache_creation_price", sa.String(length=20), nullable=True),
        sa.Column("reasoning_price", sa.String(length=20), nullable=True),
        sa.Column(
            "missing_special_token_price_policy", sa.String(length=20), nullable=False
        ),
        sa.Column("pricing_config_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["model_config_id"], ["model_configs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_connections_profile_id", "connections", ["profile_id"], unique=False
    )
    op.create_index(
        "idx_connections_model_config_id",
        "connections",
        ["model_config_id"],
        unique=False,
    )
    op.create_index(
        "idx_connections_endpoint_id", "connections", ["endpoint_id"], unique=False
    )
    op.create_index(
        "idx_connections_is_active", "connections", ["is_active"], unique=False
    )
    op.create_index(
        "idx_connections_priority", "connections", ["priority"], unique=False
    )
    op.create_index(
        "idx_connections_profile_id", "connections", ["profile_id"], unique=False
    )
    op.create_index(
        "idx_connections_profile_model_active_priority",
        "connections",
        ["profile_id", "model_config_id", "is_active", "priority"],
        unique=False,
    )

    op.create_table(
        "user_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("report_currency_code", sa.String(length=3), nullable=False),
        sa.Column("report_currency_symbol", sa.String(length=5), nullable=False),
        sa.Column("timezone_preference", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", name="uq_user_settings_profile_id"),
    )
    op.create_index(
        "ix_user_settings_profile_id", "user_settings", ["profile_id"], unique=False
    )

    op.create_table(
        "endpoint_fx_rate_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), nullable=False),
        sa.Column("fx_rate", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "model_id", "endpoint_id", name="uq_fx_profile_model_endpoint"
        ),
    )
    op.create_index(
        "ix_endpoint_fx_rate_settings_profile_id",
        "endpoint_fx_rate_settings",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "idx_fx_endpoint_id", "endpoint_fx_rate_settings", ["endpoint_id"], unique=False
    )
    op.create_index(
        "idx_fx_profile_model_endpoint",
        "endpoint_fx_rate_settings",
        ["profile_id", "model_id", "endpoint_id"],
        unique=False,
    )

    op.create_table(
        "header_blocklist_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("match_type", sa.String(length=20), nullable=False),
        sa.Column("pattern", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "((is_system = true AND profile_id IS NULL) OR (is_system = false AND profile_id IS NOT NULL))",
            name="ck_hbr_profile_scope",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "match_type", "pattern", name="uq_hbr_profile_match_pattern"
        ),
    )
    op.create_index(
        "ix_header_blocklist_rules_profile_id",
        "header_blocklist_rules",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "idx_hbr_enabled", "header_blocklist_rules", ["enabled"], unique=False
    )
    op.create_index(
        "uq_hbr_system_match_pattern",
        "header_blocklist_rules",
        ["match_type", "pattern"],
        unique=True,
        postgresql_where=sa.text("is_system = true"),
    )

    op.create_table(
        "request_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("provider_type", sa.String(length=50), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), nullable=True),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("endpoint_base_url", sa.String(length=500), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_time_ms", sa.Integer(), nullable=False),
        sa.Column("is_stream", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("success_flag", sa.Boolean(), nullable=True),
        sa.Column("billable_flag", sa.Boolean(), nullable=True),
        sa.Column("priced_flag", sa.Boolean(), nullable=True),
        sa.Column("unpriced_reason", sa.String(length=50), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("input_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("output_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("reasoning_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("total_cost_original_micros", sa.BigInteger(), nullable=True),
        sa.Column("total_cost_user_currency_micros", sa.BigInteger(), nullable=True),
        sa.Column("currency_code_original", sa.String(length=3), nullable=True),
        sa.Column("report_currency_code", sa.String(length=3), nullable=True),
        sa.Column("report_currency_symbol", sa.String(length=5), nullable=True),
        sa.Column("fx_rate_used", sa.String(length=20), nullable=True),
        sa.Column("fx_rate_source", sa.String(length=30), nullable=True),
        sa.Column("pricing_snapshot_unit", sa.String(length=10), nullable=True),
        sa.Column("pricing_snapshot_input", sa.String(length=20), nullable=True),
        sa.Column("pricing_snapshot_output", sa.String(length=20), nullable=True),
        sa.Column("pricing_snapshot_reasoning", sa.String(length=20), nullable=True),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_input_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("cache_creation_input_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column(
            "pricing_snapshot_cache_read_input", sa.String(length=20), nullable=True
        ),
        sa.Column(
            "pricing_snapshot_cache_creation_input", sa.String(length=20), nullable=True
        ),
        sa.Column(
            "pricing_snapshot_missing_special_token_price_policy",
            sa.String(length=20),
            nullable=True,
        ),
        sa.Column("pricing_config_version_used", sa.Integer(), nullable=True),
        sa.Column("request_path", sa.String(length=500), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("endpoint_description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_request_logs_profile_id", "request_logs", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_request_logs_model_id", "request_logs", ["model_id"], unique=False
    )
    op.create_index(
        "ix_request_logs_provider_type", "request_logs", ["provider_type"], unique=False
    )
    op.create_index(
        "ix_request_logs_endpoint_id", "request_logs", ["endpoint_id"], unique=False
    )
    op.create_index(
        "ix_request_logs_connection_id", "request_logs", ["connection_id"], unique=False
    )
    op.create_index(
        "ix_request_logs_status_code", "request_logs", ["status_code"], unique=False
    )
    op.create_index(
        "ix_request_logs_created_at", "request_logs", ["created_at"], unique=False
    )
    op.create_index(
        "idx_request_logs_billable_flag",
        "request_logs",
        ["billable_flag"],
        unique=False,
    )
    op.create_index(
        "idx_request_logs_priced_flag", "request_logs", ["priced_flag"], unique=False
    )
    op.create_index(
        "idx_request_logs_profile_created_at",
        "request_logs",
        ["profile_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("request_log_id", sa.Integer(), nullable=True),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), nullable=True),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("endpoint_base_url", sa.String(length=500), nullable=True),
        sa.Column("endpoint_description", sa.Text(), nullable=True),
        sa.Column("request_method", sa.String(length=10), nullable=False),
        sa.Column("request_url", sa.String(length=2000), nullable=False),
        sa.Column("request_headers", sa.Text(), nullable=False),
        sa.Column("request_body", sa.Text(), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_headers", sa.Text(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("is_stream", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"]),
        sa.ForeignKeyConstraint(
            ["request_log_id"], ["request_logs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_logs_profile_id", "audit_logs", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_audit_logs_request_log_id", "audit_logs", ["request_log_id"], unique=True
    )
    op.create_index(
        "ix_audit_logs_provider_id", "audit_logs", ["provider_id"], unique=False
    )
    op.create_index("ix_audit_logs_model_id", "audit_logs", ["model_id"], unique=False)
    op.create_index(
        "ix_audit_logs_endpoint_id", "audit_logs", ["endpoint_id"], unique=False
    )
    op.create_index(
        "ix_audit_logs_response_status", "audit_logs", ["response_status"], unique=False
    )
    op.create_index(
        "ix_audit_logs_created_at", "audit_logs", ["created_at"], unique=False
    )
    op.create_index(
        "idx_audit_logs_connection_id", "audit_logs", ["connection_id"], unique=False
    )
    op.create_index(
        "idx_audit_logs_profile_created_at",
        "audit_logs",
        ["profile_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_audit_logs_profile_created_at", table_name="audit_logs")
    op.drop_index("idx_audit_logs_connection_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_response_status", table_name="audit_logs")
    op.drop_index("ix_audit_logs_endpoint_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_model_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_provider_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_request_log_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_profile_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("idx_request_logs_profile_created_at", table_name="request_logs")
    op.drop_index("idx_request_logs_priced_flag", table_name="request_logs")
    op.drop_index("idx_request_logs_billable_flag", table_name="request_logs")
    op.drop_index("ix_request_logs_created_at", table_name="request_logs")
    op.drop_index("ix_request_logs_status_code", table_name="request_logs")
    op.drop_index("ix_request_logs_connection_id", table_name="request_logs")
    op.drop_index("ix_request_logs_endpoint_id", table_name="request_logs")
    op.drop_index("ix_request_logs_provider_type", table_name="request_logs")
    op.drop_index("ix_request_logs_model_id", table_name="request_logs")
    op.drop_index("ix_request_logs_profile_id", table_name="request_logs")
    op.drop_table("request_logs")

    op.drop_index("uq_hbr_system_match_pattern", table_name="header_blocklist_rules")
    op.drop_index("idx_hbr_enabled", table_name="header_blocklist_rules")
    op.drop_index(
        "ix_header_blocklist_rules_profile_id", table_name="header_blocklist_rules"
    )
    op.drop_table("header_blocklist_rules")

    op.drop_index(
        "idx_fx_profile_model_endpoint", table_name="endpoint_fx_rate_settings"
    )
    op.drop_index("idx_fx_endpoint_id", table_name="endpoint_fx_rate_settings")
    op.drop_index(
        "ix_endpoint_fx_rate_settings_profile_id",
        table_name="endpoint_fx_rate_settings",
    )
    op.drop_table("endpoint_fx_rate_settings")

    op.drop_index("ix_user_settings_profile_id", table_name="user_settings")
    op.drop_table("user_settings")

    op.drop_index(
        "idx_connections_profile_model_active_priority", table_name="connections"
    )
    op.drop_index("idx_connections_profile_id", table_name="connections")
    op.drop_index("idx_connections_priority", table_name="connections")
    op.drop_index("idx_connections_is_active", table_name="connections")
    op.drop_index("idx_connections_endpoint_id", table_name="connections")
    op.drop_index("idx_connections_model_config_id", table_name="connections")
    op.drop_index("ix_connections_profile_id", table_name="connections")
    op.drop_table("connections")

    op.drop_index("ix_endpoints_profile_id", table_name="endpoints")
    op.drop_table("endpoints")

    op.drop_index("idx_model_configs_profile_model_enabled", table_name="model_configs")
    op.drop_index("ix_model_configs_profile_id", table_name="model_configs")
    op.drop_table("model_configs")

    op.drop_table("providers")

    op.drop_index("uq_profiles_single_active", table_name="profiles")
    op.drop_index("idx_profiles_deleted_at", table_name="profiles")
    op.drop_table("profiles")
