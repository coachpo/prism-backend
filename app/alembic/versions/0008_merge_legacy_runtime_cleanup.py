from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008_legacy_to_dual"
down_revision = ("0004_dual_strategy_contract", "0007_legacy_runtime_cleanup")
branch_labels = None
depends_on = None


def _has_check_constraint(
    inspector: sa.Inspector, table_name: str, constraint_name: str
) -> bool:
    return any(
        constraint.get("name") == constraint_name
        for constraint in inspector.get_check_constraints(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    strategy_columns = {
        column["name"] for column in inspector.get_columns("loadbalance_strategies")
    }
    if "routing_policy" not in strategy_columns:
        if _has_check_constraint(
            inspector,
            "loadbalance_strategies",
            "chk_loadbalance_strategies_type",
        ):
            op.drop_constraint(
                "chk_loadbalance_strategies_type",
                "loadbalance_strategies",
                type_="check",
            )

        op.alter_column(
            "loadbalance_strategies",
            "strategy_type",
            new_column_name="legacy_strategy_type",
        )
        op.alter_column(
            "loadbalance_strategies",
            "legacy_strategy_type",
            existing_type=sa.String(length=20),
            nullable=True,
        )
        op.add_column(
            "loadbalance_strategies",
            sa.Column("strategy_type", sa.String(length=20), nullable=True),
        )
        op.add_column(
            "loadbalance_strategies",
            sa.Column(
                "routing_policy",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
            ),
        )
        op.alter_column(
            "loadbalance_strategies",
            "auto_recovery",
            existing_type=postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        )
        op.execute(
            sa.text(
                "UPDATE loadbalance_strategies SET strategy_type = 'legacy' WHERE strategy_type IS NULL"
            )
        )
        op.alter_column("loadbalance_strategies", "strategy_type", nullable=False)

        inspector = sa.inspect(bind)
        if not _has_check_constraint(
            inspector,
            "loadbalance_strategies",
            "chk_loadbalance_strategies_type",
        ):
            op.create_check_constraint(
                "chk_loadbalance_strategies_type",
                "loadbalance_strategies",
                "strategy_type IN ('legacy', 'adaptive')",
            )
        if not _has_check_constraint(
            inspector,
            "loadbalance_strategies",
            "chk_loadbalance_strategies_legacy_strategy_type",
        ):
            op.create_check_constraint(
                "chk_loadbalance_strategies_legacy_strategy_type",
                "loadbalance_strategies",
                "legacy_strategy_type IN ('single', 'fill-first', 'round-robin') OR legacy_strategy_type IS NULL",
            )
        if not _has_check_constraint(
            inspector,
            "loadbalance_strategies",
            "chk_loadbalance_strategies_shape",
        ):
            op.create_check_constraint(
                "chk_loadbalance_strategies_shape",
                "loadbalance_strategies",
                "((strategy_type = 'legacy' AND legacy_strategy_type IS NOT NULL AND auto_recovery IS NOT NULL AND routing_policy IS NULL) OR (strategy_type = 'adaptive' AND legacy_strategy_type IS NULL AND auto_recovery IS NULL AND routing_policy IS NOT NULL))",
            )

    runtime_columns = {
        column["name"]
        for column in inspector.get_columns("routing_connection_runtime_state")
    }
    blocked_until_column = "open_until_at"
    if "blocked_until_at" in runtime_columns and "open_until_at" not in runtime_columns:
        op.alter_column(
            "routing_connection_runtime_state",
            "blocked_until_at",
            new_column_name="open_until_at",
        )
        runtime_columns.remove("blocked_until_at")
        runtime_columns.add("open_until_at")
    if "probe_eligible_logged" not in runtime_columns:
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column(
                "probe_eligible_logged",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column(
                "circuit_state",
                sa.String(length=20),
                nullable=False,
                server_default="closed",
            ),
        )
        op.execute(
            sa.text(
                f"""
                UPDATE routing_connection_runtime_state
                SET circuit_state = CASE
                    WHEN {blocked_until_column} IS NOT NULL AND {blocked_until_column} > CURRENT_TIMESTAMP THEN 'open'
                    ELSE 'closed'
                END
                """
            )
        )
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column("probe_available_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.execute(
            sa.text(
                f"""
                UPDATE routing_connection_runtime_state
                SET probe_available_at = {blocked_until_column}
                WHERE {blocked_until_column} IS NOT NULL AND probe_available_at IS NULL
                """
            )
        )
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column("live_p95_latency_ms", sa.Integer(), nullable=True),
        )
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column("last_live_failure_kind", sa.String(length=50), nullable=True),
        )
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column(
                "last_live_failure_at", sa.DateTime(timezone=True), nullable=True
            ),
        )
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column(
                "last_live_success_at", sa.DateTime(timezone=True), nullable=True
            ),
        )
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column("last_probe_status", sa.String(length=20), nullable=True),
        )
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column("endpoint_ping_ewma_ms", sa.Numeric(10, 2), nullable=True),
        )
        op.add_column(
            "routing_connection_runtime_state",
            sa.Column("conversation_delay_ewma_ms", sa.Numeric(10, 2), nullable=True),
        )
        inspector = sa.inspect(bind)
        if not _has_check_constraint(
            inspector,
            "routing_connection_runtime_state",
            "ck_rt_state_circuit_state",
        ):
            op.create_check_constraint(
                "ck_rt_state_circuit_state",
                "routing_connection_runtime_state",
                "circuit_state IN ('closed', 'open', 'half_open')",
            )


def downgrade() -> None:
    raise NotImplementedError("Legacy runtime cleanup merge is forward-only")
