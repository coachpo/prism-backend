from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_relax_legacy_strategy"
down_revision = "0008_legacy_to_dual"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    strategy_columns = {
        column["name"]: column
        for column in inspector.get_columns("loadbalance_strategies")
    }
    legacy_strategy_column = strategy_columns.get("legacy_strategy_type")
    if legacy_strategy_column is None:
        return
    if legacy_strategy_column.get("nullable", True):
        return

    op.alter_column(
        "loadbalance_strategies",
        "legacy_strategy_type",
        existing_type=sa.String(length=20),
        nullable=True,
    )


def downgrade() -> None:
    raise NotImplementedError("Legacy strategy nullable repair is forward-only")
