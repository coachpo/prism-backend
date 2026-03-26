from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0020_vendor_api_family_split"
down_revision = "0019_lb_fill_first_strategy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("providers", "vendors")

    op.add_column("vendors", sa.Column("key", sa.String(length=100), nullable=True))
    op.execute(
        """
        UPDATE vendors
        SET name = CASE
                WHEN provider_type = 'gemini' THEN 'Google'
                ELSE name
            END,
            key = CASE
                WHEN provider_type = 'openai' THEN 'openai'
                WHEN provider_type = 'anthropic' THEN 'anthropic'
                WHEN provider_type = 'gemini' THEN 'google'
                ELSE COALESCE(
                    NULLIF(
                        btrim(
                            regexp_replace(lower(name), '[^a-z0-9]+', '-', 'g'),
                            '-'
                        ),
                        ''
                    ),
                    'vendor'
                ) || '-' || id::text
            END
        """
    )
    op.alter_column(
        "vendors",
        "key",
        existing_type=sa.String(length=100),
        nullable=False,
    )
    op.create_unique_constraint("uq_vendors_key", "vendors", ["key"])

    op.alter_column(
        "model_configs",
        "provider_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        new_column_name="vendor_id",
    )
    op.alter_column(
        "audit_logs",
        "provider_id",
        existing_type=sa.Integer(),
        existing_nullable=False,
        new_column_name="vendor_id",
    )
    op.execute(
        "ALTER INDEX IF EXISTS ix_audit_logs_provider_id RENAME TO ix_audit_logs_vendor_id"
    )
    op.alter_column(
        "loadbalance_events",
        "provider_id",
        existing_type=sa.Integer(),
        existing_nullable=True,
        new_column_name="vendor_id",
    )

    op.add_column(
        "model_configs",
        sa.Column("api_family", sa.String(length=50), nullable=True),
    )
    op.execute(
        """
        UPDATE model_configs AS model_configs
        SET api_family = vendors.provider_type
        FROM vendors
        WHERE vendors.id = model_configs.vendor_id
        """
    )
    op.alter_column(
        "model_configs",
        "api_family",
        existing_type=sa.String(length=50),
        nullable=False,
    )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_model_proxy_targets_validate_models ON model_configs"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_model_proxy_target_models()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_model_proxy_targets_validate_row ON model_proxy_targets"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_model_proxy_target_row()")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_model_proxy_target_row()
        RETURNS trigger AS $$
        DECLARE
            source_profile_id INTEGER;
            source_api_family VARCHAR(50);
            source_model_type VARCHAR(20);
            target_profile_id INTEGER;
            target_api_family VARCHAR(50);
            target_model_type VARCHAR(20);
        BEGIN
            SELECT profile_id, api_family, model_type
            INTO source_profile_id, source_api_family, source_model_type
            FROM model_configs
            WHERE id = NEW.source_model_config_id;

            SELECT profile_id, api_family, model_type
            INTO target_profile_id, target_api_family, target_model_type
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
            IF source_api_family <> target_api_family THEN
                RAISE EXCEPTION 'Proxy target source and destination must use the same api_family';
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
                JOIN model_configs source_model
                  ON source_model.id = proxy_targets.source_model_config_id
                JOIN model_configs target_model
                  ON target_model.id = proxy_targets.target_model_config_id
                WHERE (
                    proxy_targets.source_model_config_id = NEW.id
                    OR proxy_targets.target_model_config_id = NEW.id
                )
                  AND (
                    source_model.id = target_model.id
                    OR source_model.model_type <> 'proxy'
                    OR target_model.model_type <> 'native'
                     OR source_model.api_family <> target_model.api_family
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
        AFTER UPDATE OF profile_id, api_family, model_type ON model_configs
        DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW
        EXECUTE FUNCTION validate_model_proxy_target_models();
        """
    )

    op.add_column("request_logs", sa.Column("vendor_id", sa.Integer(), nullable=True))
    op.add_column(
        "request_logs",
        sa.Column("vendor_key", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "request_logs",
        sa.Column("vendor_name", sa.String(length=100), nullable=True),
    )
    op.create_index("ix_request_logs_vendor_id", "request_logs", ["vendor_id"])
    op.alter_column(
        "request_logs",
        "provider_type",
        existing_type=sa.String(length=50),
        existing_nullable=False,
        new_column_name="api_family",
    )
    op.execute(
        "ALTER INDEX IF EXISTS ix_request_logs_provider_type RENAME TO ix_request_logs_api_family"
    )
    op.execute(
        """
        WITH canonical_vendors AS (
            SELECT DISTINCT ON (provider_type)
                id,
                provider_type,
                key,
                name
            FROM vendors
            ORDER BY provider_type, id
        )
        UPDATE request_logs AS request_logs
        SET vendor_id = canonical_vendors.id,
            vendor_key = canonical_vendors.key,
            vendor_name = canonical_vendors.name
        FROM canonical_vendors
        WHERE canonical_vendors.provider_type = request_logs.api_family
        """
    )

    op.drop_column("vendors", "provider_type")


def downgrade() -> None:
    raise NotImplementedError("vendor_api_family_split is a one-way migration")
