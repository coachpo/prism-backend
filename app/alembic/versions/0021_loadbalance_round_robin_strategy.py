from __future__ import annotations

from alembic import op


revision = "0021_lb_round_robin_strategy"
down_revision = "0020_vendor_api_family_split"
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
        "strategy_type IN ('single', 'fill-first', 'round-robin', 'failover')",
    )


def downgrade() -> None:
    existing_round_robin = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM loadbalance_strategies WHERE strategy_type = 'round-robin' LIMIT 1"
        )
        .scalar()
    )
    if existing_round_robin is not None:
        raise RuntimeError(
            "Cannot downgrade 0021_loadbalance_round_robin_strategy while round-robin strategies exist"
        )

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
