from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0012_pk_seq_repair"
down_revision = "0011_observability_unlogged"
branch_labels = None
depends_on = None


LEGACY_INTEGER_PK_TABLES = (
    "audit_logs",
    "connections",
    "endpoint_fx_rate_settings",
    "endpoints",
    "header_blocklist_rules",
    "model_configs",
    "pricing_templates",
    "profiles",
    "providers",
    "request_logs",
    "user_settings",
)


def _repair_pk_sequence(*, table_name: str) -> None:
    bind = op.get_bind()
    sequence_name = f"{table_name}_id_seq"

    bind.execute(sa.text(f'CREATE SEQUENCE IF NOT EXISTS "{sequence_name}"'))
    bind.execute(
        sa.text(
            f'ALTER TABLE "{table_name}" ALTER COLUMN "id" '
            f"SET DEFAULT nextval('{sequence_name}')"
        )
    )
    bind.execute(
        sa.text(f'ALTER SEQUENCE "{sequence_name}" OWNED BY "{table_name}"."id"')
    )

    max_id = bind.execute(
        sa.text(f'SELECT COALESCE(MAX(id), 0) FROM "{table_name}"')
    ).scalar_one()

    if max_id > 0:
        bind.execute(
            sa.text(f"SELECT setval('{sequence_name}', :value, true)"),
            {"value": max_id},
        )
        return

    bind.execute(sa.text(f"SELECT setval('{sequence_name}', 1, false)"))


def upgrade() -> None:
    for table_name in LEGACY_INTEGER_PK_TABLES:
        _repair_pk_sequence(table_name=table_name)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(LEGACY_INTEGER_PK_TABLES):
        sequence_name = f"{table_name}_id_seq"
        bind.execute(
            sa.text(f'ALTER TABLE "{table_name}" ALTER COLUMN "id" DROP DEFAULT')
        )
        bind.execute(sa.text(f'DROP SEQUENCE IF EXISTS "{sequence_name}"'))
