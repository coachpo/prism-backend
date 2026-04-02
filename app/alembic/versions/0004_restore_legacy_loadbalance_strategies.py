from __future__ import annotations

revision = "0004_legacy_restore"
down_revision = "0003_connection_probe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("Legacy revision placeholders are forward-only")
