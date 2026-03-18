from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0008_loadbalance_events"
down_revision = "0007_refresh_session_duration"
branch_labels = None
depends_on = None


def _ensure_primary_key(table_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    pk_constraint = inspector.get_pk_constraint(table_name)
    pk_columns = list(pk_constraint.get("constrained_columns") or [])
    if pk_columns == ["id"]:
        return
    if pk_columns:
        raise RuntimeError(
            f"{table_name} table has unexpected primary key columns: {pk_columns}"
        )

    null_id_count = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {table_name} WHERE id IS NULL")
    ).scalar_one()
    if null_id_count:
        raise RuntimeError(
            f"Cannot create primary key on {table_name}.id because NULL ids exist"
        )

    duplicate_ids = (
        bind.execute(
            sa.text(
                f"SELECT id FROM {table_name} GROUP BY id HAVING COUNT(*) > 1 ORDER BY id LIMIT 5"
            )
        )
        .scalars()
        .all()
    )
    if duplicate_ids:
        raise RuntimeError(
            f"Cannot create primary key on {table_name}.id because duplicate ids exist: {duplicate_ids}"
        )

    op.alter_column(table_name, "id", existing_type=sa.Integer(), nullable=False)
    op.create_primary_key(f"pk_{table_name}", table_name, ["id"])


def upgrade() -> None:
    _ensure_primary_key("profiles")
    _ensure_primary_key("providers")

    op.create_table(
        "loadbalance_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("failure_kind", sa.String(length=20), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column(
            "cooldown_seconds", sa.Numeric(precision=10, scale=2), nullable=False
        ),
        sa.Column(
            "blocked_until_mono", sa.Numeric(precision=20, scale=6), nullable=True
        ),
        sa.Column("model_id", sa.String(length=200), nullable=True),
        sa.Column("endpoint_id", sa.Integer(), nullable=True),
        sa.Column("provider_id", sa.Integer(), nullable=True),
        sa.Column("failure_threshold", sa.Integer(), nullable=True),
        sa.Column(
            "backoff_multiplier", sa.Numeric(precision=5, scale=2), nullable=True
        ),
        sa.Column("max_cooldown_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(NOW() AT TIME ZONE 'UTC')"),
        ),
        sa.CheckConstraint(
            "event_type IN ('opened', 'extended', 'probe_eligible', 'recovered', 'not_opened')",
            name="chk_event_type",
        ),
        sa.CheckConstraint(
            "failure_kind IN ('transient_http', 'auth_like', 'connect_error', 'timeout') OR failure_kind IS NULL",
            name="chk_failure_kind",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            name="fk_loadbalance_events_profile_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name="fk_loadbalance_events_provider_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_loadbalance_events"),
    )

    # Standard B-tree indexes
    op.create_index(
        "idx_loadbalance_events_profile_id",
        "loadbalance_events",
        ["profile_id"],
    )
    op.create_index(
        "idx_loadbalance_events_profile_created",
        "loadbalance_events",
        ["profile_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_loadbalance_events_connection",
        "loadbalance_events",
        ["connection_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_loadbalance_events_event_type",
        "loadbalance_events",
        ["event_type"],
    )
    op.create_index(
        "idx_loadbalance_events_created_at",
        "loadbalance_events",
        ["created_at"],
    )

    # BRIN index for time-series optimization
    op.execute(
        "CREATE INDEX idx_loadbalance_events_created_brin ON loadbalance_events USING BRIN(created_at)"
    )


def downgrade() -> None:
    op.drop_table("loadbalance_events")
