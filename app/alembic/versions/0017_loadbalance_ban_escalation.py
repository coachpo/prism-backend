from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0017_loadbalance_ban_escalation"
down_revision = "0016_db_limiter_request_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "loadbalance_strategies",
        sa.Column(
            "failover_ban_mode",
            sa.String(length=20),
            nullable=False,
            server_default="off",
        ),
    )
    op.add_column(
        "loadbalance_strategies",
        sa.Column(
            "failover_max_cooldown_strikes_before_ban",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "loadbalance_strategies",
        sa.Column(
            "failover_ban_duration_seconds",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "chk_loadbalance_strategies_ban_mode",
        "loadbalance_strategies",
        "failover_ban_mode IN ('off', 'temporary', 'manual')",
    )

    op.add_column(
        "loadbalance_current_state",
        sa.Column(
            "max_cooldown_strikes",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "loadbalance_current_state",
        sa.Column(
            "ban_mode",
            sa.String(length=20),
            nullable=False,
            server_default="off",
        ),
    )
    op.add_column(
        "loadbalance_current_state",
        sa.Column("banned_until_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "chk_loadbalance_current_state_max_cooldown_strikes_nonnegative",
        "loadbalance_current_state",
        "max_cooldown_strikes >= 0",
    )
    op.create_check_constraint(
        "chk_loadbalance_current_state_ban_mode",
        "loadbalance_current_state",
        "ban_mode IN ('off', 'temporary', 'manual')",
    )

    op.drop_constraint("chk_event_type", "loadbalance_events", type_="check")
    op.create_check_constraint(
        "chk_event_type",
        "loadbalance_events",
        "event_type IN ('opened', 'extended', 'probe_eligible', 'recovered', 'not_opened', 'max_cooldown_strike', 'banned')",
    )
    op.add_column(
        "loadbalance_events",
        sa.Column("max_cooldown_strikes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "loadbalance_events",
        sa.Column("ban_mode", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "loadbalance_events",
        sa.Column("banned_until_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "chk_loadbalance_events_ban_mode",
        "loadbalance_events",
        "ban_mode IN ('off', 'temporary', 'manual') OR ban_mode IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "chk_loadbalance_events_ban_mode",
        "loadbalance_events",
        type_="check",
    )
    op.drop_column("loadbalance_events", "banned_until_at")
    op.drop_column("loadbalance_events", "ban_mode")
    op.drop_column("loadbalance_events", "max_cooldown_strikes")
    op.drop_constraint("chk_event_type", "loadbalance_events", type_="check")
    op.create_check_constraint(
        "chk_event_type",
        "loadbalance_events",
        "event_type IN ('opened', 'extended', 'probe_eligible', 'recovered', 'not_opened')",
    )

    op.drop_constraint(
        "chk_loadbalance_current_state_ban_mode",
        "loadbalance_current_state",
        type_="check",
    )
    op.drop_constraint(
        "chk_loadbalance_current_state_max_cooldown_strikes_nonnegative",
        "loadbalance_current_state",
        type_="check",
    )
    op.drop_column("loadbalance_current_state", "banned_until_at")
    op.drop_column("loadbalance_current_state", "ban_mode")
    op.drop_column("loadbalance_current_state", "max_cooldown_strikes")

    op.drop_constraint(
        "chk_loadbalance_strategies_ban_mode",
        "loadbalance_strategies",
        type_="check",
    )
    op.drop_column(
        "loadbalance_strategies",
        "failover_ban_duration_seconds",
    )
    op.drop_column(
        "loadbalance_strategies",
        "failover_max_cooldown_strikes_before_ban",
    )
    op.drop_column("loadbalance_strategies", "failover_ban_mode")
