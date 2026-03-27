from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0024_usage_request_events"
down_revision = "0023_lb_failover_status_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_logs", sa.Column("proxy_api_key_id", sa.Integer(), nullable=True)
    )
    op.add_column(
        "request_logs",
        sa.Column("proxy_api_key_name_snapshot", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "ix_request_logs_proxy_api_key_id",
        "request_logs",
        ["proxy_api_key_id"],
    )
    op.create_foreign_key(
        "fk_request_logs_proxy_api_key_id",
        "request_logs",
        "proxy_api_keys",
        ["proxy_api_key_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "usage_request_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("ingress_request_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("resolved_target_model_id", sa.String(length=200), nullable=True),
        sa.Column("api_family", sa.String(length=50), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), nullable=True),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("proxy_api_key_id", sa.Integer(), nullable=True),
        sa.Column("proxy_api_key_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("success_flag", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("input_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("output_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("cache_read_input_cost_micros", sa.BigInteger(), nullable=True),
        sa.Column("cache_creation_input_cost_micros", sa.BigInteger(), nullable=True),
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
        sa.Column(
            "pricing_snapshot_cache_read_input", sa.String(length=20), nullable=True
        ),
        sa.Column(
            "pricing_snapshot_cache_creation_input",
            sa.String(length=20),
            nullable=True,
        ),
        sa.Column("pricing_snapshot_reasoning", sa.String(length=20), nullable=True),
        sa.Column(
            "pricing_snapshot_missing_special_token_price_policy",
            sa.String(length=20),
            nullable=True,
        ),
        sa.Column("pricing_config_version_used", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("request_path", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 1",
            name="ck_usage_request_events_attempt_count_positive",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            name="fk_usage_request_events_profile_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proxy_api_key_id"],
            ["proxy_api_keys.id"],
            name="fk_usage_request_events_proxy_api_key_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_usage_request_events"),
        sa.UniqueConstraint(
            "profile_id",
            "ingress_request_id",
            name="uq_usage_request_events_profile_ingress_request",
        ),
    )
    op.create_index(
        "idx_usage_request_events_ingress_request_id",
        "usage_request_events",
        ["ingress_request_id"],
    )
    op.create_index(
        "idx_usage_request_events_profile_created_at",
        "usage_request_events",
        ["profile_id", "created_at"],
    )
    op.create_index(
        "ix_usage_request_events_profile_id",
        "usage_request_events",
        ["profile_id"],
    )
    op.create_index(
        "ix_usage_request_events_model_id",
        "usage_request_events",
        ["model_id"],
    )
    op.create_index(
        "ix_usage_request_events_api_family",
        "usage_request_events",
        ["api_family"],
    )
    op.create_index(
        "ix_usage_request_events_endpoint_id",
        "usage_request_events",
        ["endpoint_id"],
    )
    op.create_index(
        "ix_usage_request_events_connection_id",
        "usage_request_events",
        ["connection_id"],
    )
    op.create_index(
        "ix_usage_request_events_proxy_api_key_id",
        "usage_request_events",
        ["proxy_api_key_id"],
    )
    op.create_index(
        "ix_usage_request_events_created_at",
        "usage_request_events",
        ["created_at"],
    )
    op.execute("ALTER TABLE usage_request_events SET UNLOGGED")


def downgrade() -> None:
    op.drop_index(
        "ix_usage_request_events_created_at", table_name="usage_request_events"
    )
    op.drop_index(
        "ix_usage_request_events_proxy_api_key_id",
        table_name="usage_request_events",
    )
    op.drop_index(
        "ix_usage_request_events_connection_id",
        table_name="usage_request_events",
    )
    op.drop_index(
        "ix_usage_request_events_endpoint_id", table_name="usage_request_events"
    )
    op.drop_index(
        "ix_usage_request_events_api_family", table_name="usage_request_events"
    )
    op.drop_index("ix_usage_request_events_model_id", table_name="usage_request_events")
    op.drop_index(
        "ix_usage_request_events_profile_id", table_name="usage_request_events"
    )
    op.drop_index(
        "idx_usage_request_events_profile_created_at",
        table_name="usage_request_events",
    )
    op.drop_index(
        "idx_usage_request_events_ingress_request_id",
        table_name="usage_request_events",
    )
    op.drop_table("usage_request_events")

    op.drop_constraint(
        "fk_request_logs_proxy_api_key_id",
        "request_logs",
        type_="foreignkey",
    )
    op.drop_index("ix_request_logs_proxy_api_key_id", table_name="request_logs")
    op.drop_column("request_logs", "proxy_api_key_name_snapshot")
    op.drop_column("request_logs", "proxy_api_key_id")
