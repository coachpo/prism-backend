from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0016_db_limiter_request_tracking"
down_revision = "0015_lb_strategy_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("connections", sa.Column("qps_limit", sa.Integer(), nullable=True))
    op.add_column(
        "connections",
        sa.Column("max_in_flight_non_stream", sa.Integer(), nullable=True),
    )
    op.add_column(
        "connections",
        sa.Column("max_in_flight_stream", sa.Integer(), nullable=True),
    )

    op.add_column(
        "request_logs",
        sa.Column("ingress_request_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "request_logs", sa.Column("attempt_number", sa.Integer(), nullable=True)
    )
    op.add_column(
        "request_logs",
        sa.Column("provider_correlation_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "idx_request_logs_ingress_request_id",
        "request_logs",
        ["ingress_request_id"],
    )

    op.create_table(
        "connection_limiter_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "window_request_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "in_flight_non_stream",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "in_flight_stream",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(NOW() AT TIME ZONE 'UTC')"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(NOW() AT TIME ZONE 'UTC')"),
        ),
        sa.CheckConstraint(
            "window_request_count >= 0",
            name="ck_connection_limiter_state_window_request_count_nonnegative",
        ),
        sa.CheckConstraint(
            "in_flight_non_stream >= 0",
            name="ck_connection_limiter_state_non_stream_nonnegative",
        ),
        sa.CheckConstraint(
            "in_flight_stream >= 0",
            name="ck_connection_limiter_state_stream_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name="fk_connection_limiter_state_connection_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            name="fk_connection_limiter_state_profile_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_connection_limiter_state"),
        sa.UniqueConstraint(
            "profile_id",
            "connection_id",
            name="uq_connection_limiter_state_profile_connection",
        ),
        prefixes=["UNLOGGED"],
    )
    op.create_index(
        "idx_connection_limiter_state_profile_connection",
        "connection_limiter_state",
        ["profile_id", "connection_id"],
    )

    op.create_table(
        "connection_limiter_leases",
        sa.Column("lease_token", sa.String(length=64), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("lease_kind", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(NOW() AT TIME ZONE 'UTC')"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(NOW() AT TIME ZONE 'UTC')"),
        ),
        sa.CheckConstraint(
            "lease_kind IN ('stream', 'non_stream')",
            name="ck_connection_limiter_leases_kind",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name="fk_connection_limiter_leases_connection_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            name="fk_connection_limiter_leases_profile_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("lease_token", name="pk_connection_limiter_leases"),
        prefixes=["UNLOGGED"],
    )
    op.create_index(
        "idx_connection_limiter_leases_profile_connection",
        "connection_limiter_leases",
        ["profile_id", "connection_id"],
    )
    op.create_index(
        "idx_connection_limiter_leases_expires_at",
        "connection_limiter_leases",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_connection_limiter_leases_expires_at",
        table_name="connection_limiter_leases",
    )
    op.drop_index(
        "idx_connection_limiter_leases_profile_connection",
        table_name="connection_limiter_leases",
    )
    op.drop_table("connection_limiter_leases")

    op.drop_index(
        "idx_connection_limiter_state_profile_connection",
        table_name="connection_limiter_state",
    )
    op.drop_table("connection_limiter_state")

    op.drop_index("idx_request_logs_ingress_request_id", table_name="request_logs")
    op.drop_column("request_logs", "provider_correlation_id")
    op.drop_column("request_logs", "attempt_number")
    op.drop_column("request_logs", "ingress_request_id")

    op.drop_column("connections", "max_in_flight_stream")
    op.drop_column("connections", "max_in_flight_non_stream")
    op.drop_column("connections", "qps_limit")
