from __future__ import annotations

revision = "0007_legacy_runtime_cleanup"
down_revision = "0006_legacy_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("Legacy revision placeholders are forward-only")
