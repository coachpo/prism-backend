from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0023_lb_failover_status_codes"
down_revision = "0022_lb_round_robin_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "loadbalance_strategies",
        sa.Column("failover_status_codes", sa.ARRAY(sa.Integer()), nullable=True),
    )
    op.execute(
        "UPDATE loadbalance_strategies "
        "SET failover_status_codes = ARRAY[403,422,429,500,502,503,504,529]"
    )
    op.alter_column(
        "loadbalance_strategies",
        "failover_status_codes",
        nullable=False,
    )
    op.drop_column("loadbalance_strategies", "failover_auth_error_cooldown_seconds")

    op.execute(
        "UPDATE loadbalance_events SET failure_kind = 'transient_http' "
        "WHERE failure_kind = 'auth_like'"
    )
    op.execute(
        "UPDATE loadbalance_current_state SET last_failure_kind = 'transient_http' "
        "WHERE last_failure_kind = 'auth_like'"
    )

    op.drop_constraint("chk_failure_kind", "loadbalance_events", type_="check")
    op.create_check_constraint(
        "chk_failure_kind",
        "loadbalance_events",
        "failure_kind IN ('transient_http', 'connect_error', 'timeout') OR failure_kind IS NULL",
    )

    op.drop_constraint(
        "chk_loadbalance_current_state_failure_kind",
        "loadbalance_current_state",
        type_="check",
    )
    op.create_check_constraint(
        "chk_loadbalance_current_state_failure_kind",
        "loadbalance_current_state",
        "last_failure_kind IN ('transient_http', 'connect_error', 'timeout') OR last_failure_kind IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "chk_loadbalance_current_state_failure_kind",
        "loadbalance_current_state",
        type_="check",
    )
    op.create_check_constraint(
        "chk_loadbalance_current_state_failure_kind",
        "loadbalance_current_state",
        "last_failure_kind IN ('transient_http', 'auth_like', 'connect_error', 'timeout') OR last_failure_kind IS NULL",
    )

    op.drop_constraint("chk_failure_kind", "loadbalance_events", type_="check")
    op.create_check_constraint(
        "chk_failure_kind",
        "loadbalance_events",
        "failure_kind IN ('transient_http', 'auth_like', 'connect_error', 'timeout') OR failure_kind IS NULL",
    )

    op.add_column(
        "loadbalance_strategies",
        sa.Column("failover_auth_error_cooldown_seconds", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE loadbalance_strategies SET failover_auth_error_cooldown_seconds = 1800"
    )
    op.drop_column("loadbalance_strategies", "failover_status_codes")
