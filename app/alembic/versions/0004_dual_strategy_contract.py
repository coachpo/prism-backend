from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_dual_strategy_contract"
down_revision = "0003_connection_probe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    strategy_columns = {
        column["name"] for column in inspector.get_columns("loadbalance_strategies")
    }
    if "strategy_type" in strategy_columns and "routing_policy" not in strategy_columns:
        return

    op.add_column(
        "loadbalance_strategies",
        sa.Column("strategy_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "loadbalance_strategies",
        sa.Column("legacy_strategy_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "loadbalance_strategies",
        sa.Column(
            "auto_recovery",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.alter_column("loadbalance_strategies", "routing_policy", nullable=True)
    op.execute(
        sa.text(
            """
            UPDATE loadbalance_strategies
            SET strategy_type = 'adaptive'
            WHERE strategy_type IS NULL
            """
        )
    )
    op.alter_column("loadbalance_strategies", "strategy_type", nullable=False)
    op.create_check_constraint(
        "chk_loadbalance_strategies_type",
        "loadbalance_strategies",
        "strategy_type IN ('legacy', 'adaptive')",
    )
    op.create_check_constraint(
        "chk_loadbalance_strategies_legacy_strategy_type",
        "loadbalance_strategies",
        "legacy_strategy_type IN ('single', 'fill-first', 'round-robin') OR legacy_strategy_type IS NULL",
    )
    op.create_check_constraint(
        "chk_loadbalance_strategies_shape",
        "loadbalance_strategies",
        "((strategy_type = 'legacy' AND legacy_strategy_type IS NOT NULL AND auto_recovery IS NOT NULL AND routing_policy IS NULL) OR (strategy_type = 'adaptive' AND legacy_strategy_type IS NULL AND auto_recovery IS NULL AND routing_policy IS NOT NULL))",
    )
    if not inspector.has_table("loadbalance_round_robin_state"):
        op.create_table(
            "loadbalance_round_robin_state",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("profile_id", sa.Integer(), nullable=False),
            sa.Column("model_config_id", sa.Integer(), nullable=False),
            sa.Column("next_cursor", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.CheckConstraint(
                "next_cursor >= 0",
                name="ck_loadbalance_round_robin_state_next_cursor_nonnegative",
            ),
            sa.ForeignKeyConstraint(
                ["model_config_id"], ["model_configs.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "profile_id",
                "model_config_id",
                name="uq_loadbalance_round_robin_state_profile_model",
            ),
        )
        op.create_index(
            "idx_loadbalance_round_robin_state_profile_model",
            "loadbalance_round_robin_state",
            ["profile_id", "model_config_id"],
            unique=False,
        )


def downgrade() -> None:
    raise NotImplementedError("Dual strategy contract migration is forward-only")
