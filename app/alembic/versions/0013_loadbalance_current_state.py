from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_lb_current_state"
down_revision = "0012_pk_seq_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "loadbalance_current_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_failure_kind", sa.String(length=20), nullable=True),
        sa.Column(
            "last_cooldown_seconds",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("blocked_until_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "probe_eligible_logged",
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
            "last_failure_kind IN ('transient_http', 'auth_like', 'connect_error', 'timeout') OR last_failure_kind IS NULL",
            name="chk_loadbalance_current_state_failure_kind",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name="fk_loadbalance_current_state_connection_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            name="fk_loadbalance_current_state_profile_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_loadbalance_current_state"),
        sa.UniqueConstraint(
            "profile_id",
            "connection_id",
            name="uq_loadbalance_current_state_profile_connection",
        ),
        prefixes=["UNLOGGED"],
    )

    op.create_index(
        "idx_loadbalance_current_state_profile_id",
        "loadbalance_current_state",
        ["profile_id"],
    )
    op.create_index(
        "idx_loadbalance_current_state_connection_id",
        "loadbalance_current_state",
        ["connection_id"],
    )
    op.create_index(
        "idx_loadbalance_current_state_profile_connection",
        "loadbalance_current_state",
        ["profile_id", "connection_id"],
    )
    op.create_index(
        "idx_loadbalance_current_state_created_at",
        "loadbalance_current_state",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("loadbalance_current_state")
