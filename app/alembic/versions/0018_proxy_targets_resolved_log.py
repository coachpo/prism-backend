from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0018_proxy_targets_resolved_log"
down_revision = "0017_loadbalance_ban_escalation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_proxy_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_model_config_id", sa.Integer(), nullable=False),
        sa.Column("target_model_config_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_model_config_id"],
            ["model_configs.id"],
            name="fk_model_proxy_targets_source_model_config_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_model_config_id"],
            ["model_configs.id"],
            name="fk_model_proxy_targets_target_model_config_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_model_proxy_targets"),
        sa.UniqueConstraint(
            "source_model_config_id",
            "position",
            name="uq_model_proxy_targets_source_position",
        ),
        sa.UniqueConstraint(
            "source_model_config_id",
            "target_model_config_id",
            name="uq_model_proxy_targets_source_target",
        ),
    )
    op.create_index(
        "idx_model_proxy_targets_source_position",
        "model_proxy_targets",
        ["source_model_config_id", "position"],
    )
    op.create_index(
        "idx_model_proxy_targets_target_model",
        "model_proxy_targets",
        ["target_model_config_id"],
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_model_proxy_target_row()
        RETURNS trigger AS $$
        DECLARE
            source_profile_id INTEGER;
            source_provider_id INTEGER;
            source_model_type VARCHAR(20);
            target_profile_id INTEGER;
            target_provider_id INTEGER;
            target_model_type VARCHAR(20);
        BEGIN
            SELECT profile_id, provider_id, model_type
            INTO source_profile_id, source_provider_id, source_model_type
            FROM model_configs
            WHERE id = NEW.source_model_config_id;

            SELECT profile_id, provider_id, model_type
            INTO target_profile_id, target_provider_id, target_model_type
            FROM model_configs
            WHERE id = NEW.target_model_config_id;

            IF NEW.source_model_config_id = NEW.target_model_config_id THEN
                RAISE EXCEPTION 'Proxy target cannot reference itself';
            END IF;
            IF source_model_type <> 'proxy' THEN
                RAISE EXCEPTION 'Proxy target source must be a proxy model';
            END IF;
            IF target_model_type <> 'native' THEN
                RAISE EXCEPTION 'Proxy target destination must be a native model';
            END IF;
            IF source_provider_id <> target_provider_id THEN
                RAISE EXCEPTION 'Proxy target source and destination must use the same provider';
            END IF;
            IF source_profile_id <> target_profile_id THEN
                RAISE EXCEPTION 'Proxy target source and destination must be in the same profile';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_model_proxy_targets_validate_row
        AFTER INSERT OR UPDATE ON model_proxy_targets
        DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW
        EXECUTE FUNCTION validate_model_proxy_target_row();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_model_proxy_target_models()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM model_proxy_targets proxy_targets
                JOIN model_configs source_model ON source_model.id = proxy_targets.source_model_config_id
                JOIN model_configs target_model ON target_model.id = proxy_targets.target_model_config_id
                WHERE (
                    proxy_targets.source_model_config_id = NEW.id
                    OR proxy_targets.target_model_config_id = NEW.id
                )
                  AND (
                    source_model.id = target_model.id
                    OR source_model.model_type <> 'proxy'
                    OR target_model.model_type <> 'native'
                    OR source_model.provider_id <> target_model.provider_id
                    OR source_model.profile_id <> target_model.profile_id
                  )
            ) THEN
                RAISE EXCEPTION 'Model proxy target invariant violated by model update';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_model_proxy_targets_validate_models
        AFTER UPDATE OF profile_id, provider_id, model_type ON model_configs
        DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW
        EXECUTE FUNCTION validate_model_proxy_target_models();
        """
    )

    op.execute(
        """
        INSERT INTO model_proxy_targets (source_model_config_id, target_model_config_id, position)
        SELECT source.id, target.id, 0
        FROM model_configs AS source
        JOIN model_configs AS target
          ON target.profile_id = source.profile_id
         AND target.model_id = source.redirect_to
        WHERE source.model_type = 'proxy'
          AND source.redirect_to IS NOT NULL;
        """
    )

    op.add_column(
        "request_logs",
        sa.Column("resolved_target_model_id", sa.String(length=200), nullable=True),
    )
    op.drop_column("model_configs", "redirect_to")


def downgrade() -> None:
    op.add_column(
        "model_configs",
        sa.Column("redirect_to", sa.String(length=200), nullable=True),
    )
    op.execute(
        """
        UPDATE model_configs AS source
        SET redirect_to = target.model_id
        FROM model_proxy_targets AS proxy_targets
        JOIN model_configs AS target
          ON target.id = proxy_targets.target_model_config_id
        WHERE source.id = proxy_targets.source_model_config_id
          AND proxy_targets.position = 0;
        """
    )
    op.drop_column("request_logs", "resolved_target_model_id")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_model_proxy_targets_validate_models ON model_configs"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_model_proxy_target_models()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_model_proxy_targets_validate_row ON model_proxy_targets"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_model_proxy_target_row()")
    op.drop_index(
        "idx_model_proxy_targets_target_model",
        table_name="model_proxy_targets",
    )
    op.drop_index(
        "idx_model_proxy_targets_source_position",
        table_name="model_proxy_targets",
    )
    op.drop_table("model_proxy_targets")
