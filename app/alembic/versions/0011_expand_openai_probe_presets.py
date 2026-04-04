from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0011_expand_openai_probe_presets"
down_revision = "0010_relax_auto_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE connections
            SET openai_probe_endpoint_variant = CASE
                WHEN openai_probe_endpoint_variant = 'chat_completions' THEN 'chat_completions_minimal'
                ELSE 'responses_minimal'
            END
            WHERE openai_probe_endpoint_variant IN ('responses', 'chat_completions')
            """
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE connections ALTER COLUMN openai_probe_endpoint_variant TYPE VARCHAR(40)"
        )
    )
    op.drop_constraint(
        "ck_connections_openai_probe_endpoint_variant",
        "connections",
        type_="check",
    )
    op.create_check_constraint(
        "ck_connections_openai_probe_endpoint_variant",
        "connections",
        "openai_probe_endpoint_variant IN ('responses_minimal', 'responses_reasoning_none', 'chat_completions_minimal', 'chat_completions_reasoning_none')",
    )


def downgrade() -> None:
    raise NotImplementedError("OpenAI probe preset expansion is forward-only")
