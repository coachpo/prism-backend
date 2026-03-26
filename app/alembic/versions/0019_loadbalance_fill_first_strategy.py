from __future__ import annotations

from alembic import op


revision = "0019_lb_fill_first_strategy"
down_revision = "0018_proxy_targets_resolved_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "chk_loadbalance_strategies_type",
        "loadbalance_strategies",
        type_="check",
    )
    op.create_check_constraint(
        "chk_loadbalance_strategies_type",
        "loadbalance_strategies",
        "strategy_type IN ('single', 'fill-first', 'failover')",
    )

    op.drop_constraint(
        "chk_loadbalance_strategies_recovery",
        "loadbalance_strategies",
        type_="check",
    )
    op.create_check_constraint(
        "chk_loadbalance_strategies_recovery",
        "loadbalance_strategies",
        "strategy_type <> 'single' OR failover_recovery_enabled = false",
    )


def downgrade() -> None:
    op.drop_constraint(
        "chk_loadbalance_strategies_recovery",
        "loadbalance_strategies",
        type_="check",
    )
    op.create_check_constraint(
        "chk_loadbalance_strategies_recovery",
        "loadbalance_strategies",
        "strategy_type = 'failover' OR failover_recovery_enabled = false",
    )

    op.drop_constraint(
        "chk_loadbalance_strategies_type",
        "loadbalance_strategies",
        type_="check",
    )
    op.create_check_constraint(
        "chk_loadbalance_strategies_type",
        "loadbalance_strategies",
        "strategy_type IN ('single', 'failover')",
    )
