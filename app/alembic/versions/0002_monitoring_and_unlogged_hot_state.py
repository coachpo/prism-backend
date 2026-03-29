from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_monitoring_hot_state"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text('ALTER TABLE "request_logs" SET LOGGED'))
    op.execute(sa.text('ALTER TABLE "audit_logs" SET LOGGED'))
    op.execute(sa.text('ALTER TABLE "usage_request_events" SET LOGGED'))
    op.execute(sa.text('ALTER TABLE "loadbalance_events" SET LOGGED'))

    op.add_column(
        "loadbalance_strategies",
        sa.Column(
            "routing_policy", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE loadbalance_strategies
            SET routing_policy = jsonb_build_object(
                'kind', 'adaptive',
                'routing_objective', 'minimize_latency',
                'deadline_budget_ms', 30000,
                'hedge', jsonb_build_object(
                    'enabled', false,
                    'delay_ms', 1500,
                    'max_additional_attempts', 1
                ),
                'circuit_breaker', jsonb_build_object(
                    'failure_status_codes', COALESCE(auto_recovery->'status_codes', '[403, 422, 429, 500, 502, 503, 504, 529]'::jsonb),
                    'base_open_seconds', COALESCE((auto_recovery->'cooldown'->>'base_seconds')::integer, 60),
                    'failure_threshold', COALESCE((auto_recovery->'cooldown'->>'failure_threshold')::integer, 2),
                    'backoff_multiplier', COALESCE((auto_recovery->'cooldown'->>'backoff_multiplier')::numeric, 2.0),
                    'max_open_seconds', COALESCE((auto_recovery->'cooldown'->>'max_cooldown_seconds')::integer, 900),
                    'jitter_ratio', COALESCE((auto_recovery->'cooldown'->>'jitter_ratio')::numeric, 0.2),
                    'ban_mode', COALESCE(auto_recovery->'ban'->>'mode', 'off'),
                    'max_open_strikes_before_ban', COALESCE((auto_recovery->'ban'->>'max_cooldown_strikes_before_ban')::integer, 0),
                    'ban_duration_seconds', COALESCE((auto_recovery->'ban'->>'ban_duration_seconds')::integer, 0)
                ),
                'admission', jsonb_build_object(
                    'respect_qps_limit', true,
                    'respect_in_flight_limits', true
                ),
                'monitoring', jsonb_build_object(
                    'enabled', true,
                    'stale_after_seconds', 300,
                    'endpoint_ping_weight', 1.0,
                    'conversation_delay_weight', 1.0,
                    'failure_penalty_weight', 2.0
                )
            )
            """
        )
    )
    op.alter_column("loadbalance_strategies", "routing_policy", nullable=False)
    op.drop_column("loadbalance_strategies", "auto_recovery")
    op.drop_column("loadbalance_strategies", "strategy_type")

    op.add_column(
        "connections",
        sa.Column(
            "openai_probe_endpoint_variant",
            sa.String(length=30),
            nullable=True,
            server_default="responses",
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE connections
            SET openai_probe_endpoint_variant = 'responses'
            WHERE openai_probe_endpoint_variant IS NULL
            """
        )
    )
    op.alter_column(
        "connections",
        "openai_probe_endpoint_variant",
        nullable=False,
        server_default=None,
    )
    op.create_check_constraint(
        "ck_connections_openai_probe_endpoint_variant",
        "connections",
        "openai_probe_endpoint_variant IN ('responses', 'chat_completions')",
    )

    op.add_column(
        "user_settings",
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
            UPDATE user_settings
            SET monitoring_probe_interval_seconds = 300
            WHERE monitoring_probe_interval_seconds IS NULL
            """
        )
    )
    op.alter_column(
        "user_settings",
        "monitoring_probe_interval_seconds",
        nullable=False,
        server_default=None,
    )

    op.create_table(
        "monitoring_connection_probe_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("vendor_id", sa.Integer(), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_ping_status", sa.String(length=20), nullable=False),
        sa.Column("endpoint_ping_ms", sa.Integer(), nullable=True),
        sa.Column("conversation_status", sa.String(length=20), nullable=False),
        sa.Column("conversation_delay_ms", sa.Integer(), nullable=True),
        sa.Column("failure_kind", sa.String(length=50), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendors.id"]),
        sa.ForeignKeyConstraint(
            ["model_config_id"], ["model_configs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "endpoint_ping_status IN ('healthy', 'degraded', 'unhealthy')",
            name="ck_monitoring_connection_probe_results_endpoint_ping_status",
        ),
        sa.CheckConstraint(
            "conversation_status IN ('healthy', 'degraded', 'unhealthy')",
            name="ck_monitoring_connection_probe_results_conversation_status",
        ),
    )
    op.create_index(
        "idx_monitoring_connection_probe_results_profile_checked_at",
        "monitoring_connection_probe_results",
        ["profile_id", "checked_at"],
    )
    op.create_index(
        "idx_monitoring_connection_probe_results_connection_checked_at",
        "monitoring_connection_probe_results",
        ["connection_id", "checked_at"],
    )

    op.create_table(
        "routing_connection_runtime_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_request_count", sa.Integer(), nullable=False),
        sa.Column("in_flight_non_stream", sa.Integer(), nullable=False),
        sa.Column("in_flight_stream", sa.Integer(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_failure_kind", sa.String(length=20), nullable=True),
        sa.Column("last_cooldown_seconds", sa.Numeric(10, 2), nullable=False),
        sa.Column("max_cooldown_strikes", sa.Integer(), nullable=False),
        sa.Column("ban_mode", sa.String(length=20), nullable=False),
        sa.Column("banned_until_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open_until_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("probe_eligible_logged", sa.Boolean(), nullable=False),
        sa.Column("circuit_state", sa.String(length=20), nullable=False),
        sa.Column("probe_available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("live_p95_latency_ms", sa.Integer(), nullable=True),
        sa.Column("last_live_failure_kind", sa.String(length=50), nullable=True),
        sa.Column("last_live_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_live_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_status", sa.String(length=20), nullable=True),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("endpoint_ping_ewma_ms", sa.Numeric(10, 2), nullable=True),
        sa.Column("conversation_delay_ewma_ms", sa.Numeric(10, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "connection_id",
            name="uq_routing_connection_runtime_state_profile_connection",
        ),
        sa.CheckConstraint(
            "window_request_count >= 0",
            name="ck_rt_state_window_count_nonneg",
        ),
        sa.CheckConstraint(
            "in_flight_non_stream >= 0",
            name="ck_rt_state_non_stream_nonneg",
        ),
        sa.CheckConstraint(
            "in_flight_stream >= 0",
            name="ck_rt_state_stream_nonneg",
        ),
        sa.CheckConstraint(
            "max_cooldown_strikes >= 0",
            name="ck_rt_state_max_strikes_nonneg",
        ),
        sa.CheckConstraint(
            "ban_mode IN ('off', 'temporary', 'manual')",
            name="ck_rt_state_ban_mode",
        ),
        sa.CheckConstraint(
            "last_failure_kind IN ('transient_http', 'connect_error', 'timeout') OR last_failure_kind IS NULL",
            name="ck_rt_state_last_failure_kind",
        ),
        sa.CheckConstraint(
            "circuit_state IN ('closed', 'open', 'half_open')",
            name="ck_rt_state_circuit_state",
        ),
    )
    op.create_index(
        "idx_routing_connection_runtime_state_profile_connection",
        "routing_connection_runtime_state",
        ["profile_id", "connection_id"],
    )
    op.execute(sa.text('ALTER TABLE "routing_connection_runtime_state" SET UNLOGGED'))

    op.create_table(
        "routing_connection_runtime_leases",
        sa.Column("lease_token", sa.String(length=64), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("lease_kind", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["connections.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("lease_token"),
        sa.CheckConstraint(
            "lease_kind IN ('stream', 'non_stream', 'half_open_probe')",
            name="ck_routing_connection_runtime_leases_kind",
        ),
    )
    op.create_index(
        "idx_routing_connection_runtime_leases_profile_connection",
        "routing_connection_runtime_leases",
        ["profile_id", "connection_id"],
    )
    op.create_index(
        "idx_routing_connection_runtime_leases_expires_at",
        "routing_connection_runtime_leases",
        ["expires_at"],
    )
    op.execute(sa.text('ALTER TABLE "routing_connection_runtime_leases" SET UNLOGGED'))

    op.drop_table("connection_limiter_leases")
    op.drop_table("connection_limiter_state")
    op.drop_table("loadbalance_current_state")
    op.drop_table("loadbalance_round_robin_state")


def downgrade() -> None:
    raise NotImplementedError(
        "Monitoring and runtime hot-state migration is forward-only"
    )
