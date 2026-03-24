from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0014_lb_strategies"
down_revision = "0013_lb_current_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loadbalance_strategies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("strategy_type", sa.String(length=20), nullable=False),
        sa.Column(
            "failover_recovery_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
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
            "strategy_type IN ('single', 'failover')",
            name="chk_loadbalance_strategies_type",
        ),
        sa.CheckConstraint(
            "strategy_type = 'failover' OR failover_recovery_enabled = false",
            name="chk_loadbalance_strategies_recovery",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            name="fk_loadbalance_strategies_profile_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_loadbalance_strategies"),
        sa.UniqueConstraint(
            "profile_id",
            "name",
            name="uq_loadbalance_strategies_profile_name",
        ),
        sa.UniqueConstraint(
            "profile_id",
            "id",
            name="uq_loadbalance_strategies_profile_id_id",
        ),
    )
    op.create_index(
        "idx_loadbalance_strategies_profile_id",
        "loadbalance_strategies",
        ["profile_id"],
    )

    op.add_column(
        "model_configs",
        sa.Column("loadbalance_strategy_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "idx_model_configs_loadbalance_strategy_id",
        "model_configs",
        ["loadbalance_strategy_id"],
    )

    bind = op.get_bind()
    model_configs = sa.table(
        "model_configs",
        sa.column("id", sa.Integer()),
        sa.column("profile_id", sa.Integer()),
        sa.column("model_id", sa.String(length=200)),
        sa.column("model_type", sa.String(length=20)),
        sa.column("lb_strategy", sa.String(length=50)),
        sa.column("failover_recovery_enabled", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("loadbalance_strategy_id", sa.Integer()),
    )
    strategies = sa.table(
        "loadbalance_strategies",
        sa.column("id", sa.Integer()),
        sa.column("profile_id", sa.Integer()),
        sa.column("name", sa.String(length=200)),
        sa.column("strategy_type", sa.String(length=20)),
        sa.column("failover_recovery_enabled", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    native_rows = bind.execute(
        sa.select(
            model_configs.c.id,
            model_configs.c.profile_id,
            model_configs.c.model_id,
            model_configs.c.lb_strategy,
            model_configs.c.failover_recovery_enabled,
            model_configs.c.created_at,
            model_configs.c.updated_at,
        ).where(model_configs.c.model_type == "native")
    ).mappings()

    for row in native_rows:
        strategy_type = "failover" if row["lb_strategy"] == "failover" else "single"
        recovery_enabled = (
            bool(row["failover_recovery_enabled"])
            if strategy_type == "failover"
            else False
        )
        strategy_id = bind.execute(
            sa.insert(strategies)
            .values(
                profile_id=row["profile_id"],
                name=f"{row['model_id']}-strategy",
                strategy_type=strategy_type,
                failover_recovery_enabled=recovery_enabled,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            .returning(strategies.c.id)
        ).scalar_one()
        bind.execute(
            sa.update(model_configs)
            .where(model_configs.c.id == row["id"])
            .values(loadbalance_strategy_id=strategy_id)
        )

    op.create_foreign_key(
        "fk_model_configs_profile_loadbalance_strategy",
        "model_configs",
        "loadbalance_strategies",
        ["profile_id", "loadbalance_strategy_id"],
        ["profile_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "chk_model_configs_strategy_attachment",
        "model_configs",
        "(model_type = 'native' AND loadbalance_strategy_id IS NOT NULL) OR "
        "(model_type = 'proxy' AND loadbalance_strategy_id IS NULL)",
    )

    op.drop_column("model_configs", "lb_strategy")
    op.drop_column("model_configs", "failover_recovery_enabled")
    op.drop_column("model_configs", "failover_recovery_cooldown_seconds")


def downgrade() -> None:
    op.add_column(
        "model_configs",
        sa.Column(
            "failover_recovery_cooldown_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
    )
    op.add_column(
        "model_configs",
        sa.Column(
            "failover_recovery_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "model_configs",
        sa.Column(
            "lb_strategy",
            sa.String(length=50),
            nullable=False,
            server_default=sa.text("'single'"),
        ),
    )

    bind = op.get_bind()
    model_configs = sa.table(
        "model_configs",
        sa.column("id", sa.Integer()),
        sa.column("model_type", sa.String(length=20)),
        sa.column("loadbalance_strategy_id", sa.Integer()),
        sa.column("lb_strategy", sa.String(length=50)),
        sa.column("failover_recovery_enabled", sa.Boolean()),
        sa.column("failover_recovery_cooldown_seconds", sa.Integer()),
    )
    strategies = sa.table(
        "loadbalance_strategies",
        sa.column("id", sa.Integer()),
        sa.column("strategy_type", sa.String(length=20)),
        sa.column("failover_recovery_enabled", sa.Boolean()),
    )

    rows = bind.execute(
        sa.select(
            model_configs.c.id,
            model_configs.c.model_type,
            strategies.c.strategy_type,
            strategies.c.failover_recovery_enabled,
        ).select_from(
            model_configs.outerjoin(
                strategies,
                model_configs.c.loadbalance_strategy_id == strategies.c.id,
            )
        )
    ).mappings()

    for row in rows:
        if row["model_type"] == "proxy":
            bind.execute(
                sa.update(model_configs)
                .where(model_configs.c.id == row["id"])
                .values(
                    lb_strategy="single",
                    failover_recovery_enabled=True,
                    failover_recovery_cooldown_seconds=60,
                )
            )
            continue

        strategy_type = row["strategy_type"] or "single"
        recovery_enabled = (
            bool(row["failover_recovery_enabled"])
            if strategy_type == "failover"
            else False
        )
        bind.execute(
            sa.update(model_configs)
            .where(model_configs.c.id == row["id"])
            .values(
                lb_strategy=strategy_type,
                failover_recovery_enabled=recovery_enabled,
                failover_recovery_cooldown_seconds=60,
            )
        )

    op.drop_constraint(
        "chk_model_configs_strategy_attachment",
        "model_configs",
        type_="check",
    )
    op.drop_constraint(
        "fk_model_configs_profile_loadbalance_strategy",
        "model_configs",
        type_="foreignkey",
    )
    op.drop_index(
        "idx_model_configs_loadbalance_strategy_id", table_name="model_configs"
    )
    op.drop_column("model_configs", "loadbalance_strategy_id")

    op.drop_index(
        "idx_loadbalance_strategies_profile_id", table_name="loadbalance_strategies"
    )
    op.drop_table("loadbalance_strategies")
