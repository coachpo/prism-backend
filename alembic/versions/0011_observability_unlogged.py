from __future__ import annotations

from alembic import op


revision = "0011_observability_unlogged"
down_revision = "0010_webauthn_challenges"
branch_labels = None
depends_on = None


def _set_table_persistence(*, table_name: str, persistence: str) -> None:
    op.execute(f"ALTER TABLE {table_name} SET {persistence}")


def upgrade() -> None:
    # Keep audit_logs ahead of request_logs for the verified FK-backed transition.
    _set_table_persistence(table_name="audit_logs", persistence="UNLOGGED")
    _set_table_persistence(table_name="request_logs", persistence="UNLOGGED")
    _set_table_persistence(table_name="loadbalance_events", persistence="UNLOGGED")


def downgrade() -> None:
    # Preserve the matching downgrade order for the request_log_id FK path.
    _set_table_persistence(table_name="request_logs", persistence="LOGGED")
    _set_table_persistence(table_name="audit_logs", persistence="LOGGED")
    _set_table_persistence(table_name="loadbalance_events", persistence="LOGGED")
