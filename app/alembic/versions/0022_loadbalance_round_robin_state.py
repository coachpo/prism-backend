from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0022_lb_round_robin_state"
down_revision = "0021_lb_round_robin_strategy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loadbalance_round_robin_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("model_config_id", sa.Integer(), nullable=False),
        sa.Column(
            "next_cursor",
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
            "next_cursor >= 0",
            name="ck_loadbalance_round_robin_state_next_cursor_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["model_config_id"],
            ["model_configs.id"],
            name="fk_loadbalance_round_robin_state_model_config_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_loadbalance_round_robin_state"),
        sa.UniqueConstraint(
            "profile_id",
            "model_config_id",
            name="uq_loadbalance_round_robin_state_profile_model",
        ),
        prefixes=["UNLOGGED"],
    )
    op.create_index(
        "idx_loadbalance_round_robin_state_profile_model",
        "loadbalance_round_robin_state",
        ["profile_id", "model_config_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_loadbalance_round_robin_state_profile_model",
        table_name="loadbalance_round_robin_state",
    )
    op.drop_table("loadbalance_round_robin_state")
