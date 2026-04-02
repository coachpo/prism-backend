from __future__ import annotations

revision = "0006_legacy_runtime"
down_revision = "0005_legacy_presets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("Legacy revision placeholders are forward-only")
