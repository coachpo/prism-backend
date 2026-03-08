"""Convert all datetime columns to timezone-aware UTC.

Compatibility migration for databases created before the squashed baseline.
Fresh databases already have timezone-aware columns in ``0001_initial``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0002_utc_timestamps"
down_revision = "0001_initial_status"
branch_labels = None
depends_on = None


TIMESTAMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("profiles", "deleted_at"),
    ("profiles", "created_at"),
    ("profiles", "updated_at"),
    ("providers", "created_at"),
    ("providers", "updated_at"),
    ("model_configs", "created_at"),
    ("model_configs", "updated_at"),
    ("endpoints", "created_at"),
    ("endpoints", "updated_at"),
    ("connections", "last_health_check"),
    ("connections", "created_at"),
    ("connections", "updated_at"),
    ("user_settings", "created_at"),
    ("user_settings", "updated_at"),
    ("endpoint_fx_rate_settings", "created_at"),
    ("endpoint_fx_rate_settings", "updated_at"),
    ("header_blocklist_rules", "created_at"),
    ("header_blocklist_rules", "updated_at"),
    ("request_logs", "created_at"),
    ("audit_logs", "created_at"),
)


def _column_uses_timezone(table_name: str, column_name: str) -> bool | None:
    inspector = sa.inspect(op.get_bind())
    for column in inspector.get_columns(table_name):
        if column["name"] == column_name:
            return bool(getattr(column["type"], "timezone", False))
    return None


def upgrade() -> None:
    for table_name, column_name in TIMESTAMP_COLUMNS:
        column_uses_timezone = _column_uses_timezone(table_name, column_name)
        if column_uses_timezone in (None, True):
            continue
        op.alter_column(
            table_name,
            column_name,
            type_=sa.DateTime(timezone=True),
            postgresql_using=f"{column_name} AT TIME ZONE 'UTC'",
        )


def downgrade() -> None:
    pass
