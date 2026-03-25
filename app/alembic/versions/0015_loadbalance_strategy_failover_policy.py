from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0015_lb_strategy_policy"
down_revision = "0014_lb_strategies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "loadbalance_strategies",
        sa.Column("failover_cooldown_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "loadbalance_strategies",
        sa.Column("failover_failure_threshold", sa.Integer(), nullable=True),
    )
    op.add_column(
        "loadbalance_strategies",
        sa.Column("failover_backoff_multiplier", sa.Float(), nullable=True),
    )
    op.add_column(
        "loadbalance_strategies",
        sa.Column("failover_max_cooldown_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "loadbalance_strategies",
        sa.Column("failover_jitter_ratio", sa.Float(), nullable=True),
    )
    op.add_column(
        "loadbalance_strategies",
        sa.Column(
            "failover_auth_error_cooldown_seconds",
            sa.Integer(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("loadbalance_strategies", "failover_auth_error_cooldown_seconds")
    op.drop_column("loadbalance_strategies", "failover_jitter_ratio")
    op.drop_column("loadbalance_strategies", "failover_max_cooldown_seconds")
    op.drop_column("loadbalance_strategies", "failover_backoff_multiplier")
    op.drop_column("loadbalance_strategies", "failover_failure_threshold")
    op.drop_column("loadbalance_strategies", "failover_cooldown_seconds")
