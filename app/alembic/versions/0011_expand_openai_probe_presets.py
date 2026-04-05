from __future__ import annotations

revision = "0011_expand_openai_probe_presets"
down_revision = "0010_relax_auto_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    return None


def downgrade() -> None:
    raise NotImplementedError("OpenAI probe preset expansion is forward-only")
