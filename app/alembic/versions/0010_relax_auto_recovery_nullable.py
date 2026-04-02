from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_relax_auto_recovery"
down_revision = "0009_relax_legacy_strategy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    strategy_columns = {
        column["name"]: column
        for column in inspector.get_columns("loadbalance_strategies")
    }
    auto_recovery_column = strategy_columns.get("auto_recovery")
    if auto_recovery_column is None:
        return
    if auto_recovery_column.get("nullable", True):
        return

    op.alter_column(
        "loadbalance_strategies",
        "auto_recovery",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=True,
    )


def downgrade() -> None:
    raise NotImplementedError("Auto recovery nullable repair is forward-only")
