"""Add profiles table and profile_id columns

Revision ID: 0002_profiles_additive
Revises: 0001_initial_schema
Create Date: 2026-02-28 18:05:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "0002_profiles_additive"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("idx_profiles_deleted_at", "profiles", ["deleted_at"], unique=False)
    op.create_index(
        "uq_profiles_single_active",
        "profiles",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    op.add_column("model_configs", sa.Column("profile_id", sa.Integer(), nullable=True))
    op.add_column("endpoints", sa.Column("profile_id", sa.Integer(), nullable=True))
    op.add_column("connections", sa.Column("profile_id", sa.Integer(), nullable=True))
    op.add_column("user_settings", sa.Column("profile_id", sa.Integer(), nullable=True))
    op.add_column(
        "endpoint_fx_rate_settings",
        sa.Column("profile_id", sa.Integer(), nullable=True),
    )
    op.add_column("request_logs", sa.Column("profile_id", sa.Integer(), nullable=True))
    op.add_column("audit_logs", sa.Column("profile_id", sa.Integer(), nullable=True))
    op.add_column(
        "header_blocklist_rules",
        sa.Column("profile_id", sa.Integer(), nullable=True),
    )

    op.create_index(
        "ix_model_configs_profile_id", "model_configs", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_endpoints_profile_id", "endpoints", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_connections_profile_id", "connections", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_user_settings_profile_id", "user_settings", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_endpoint_fx_rate_settings_profile_id",
        "endpoint_fx_rate_settings",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_request_logs_profile_id", "request_logs", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_audit_logs_profile_id", "audit_logs", ["profile_id"], unique=False
    )
    op.create_index(
        "ix_header_blocklist_rules_profile_id",
        "header_blocklist_rules",
        ["profile_id"],
        unique=False,
    )

    op.create_foreign_key(
        "fk_model_configs_profile_id_profiles",
        "model_configs",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_endpoints_profile_id_profiles",
        "endpoints",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_connections_profile_id_profiles",
        "connections",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_user_settings_profile_id_profiles",
        "user_settings",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_endpoint_fx_rate_settings_profile_id_profiles",
        "endpoint_fx_rate_settings",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_request_logs_profile_id_profiles",
        "request_logs",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_audit_logs_profile_id_profiles",
        "audit_logs",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_header_blocklist_rules_profile_id_profiles",
        "header_blocklist_rules",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_header_blocklist_rules_profile_id_profiles",
        "header_blocklist_rules",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_audit_logs_profile_id_profiles",
        "audit_logs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_request_logs_profile_id_profiles",
        "request_logs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_endpoint_fx_rate_settings_profile_id_profiles",
        "endpoint_fx_rate_settings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_user_settings_profile_id_profiles",
        "user_settings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_connections_profile_id_profiles",
        "connections",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_endpoints_profile_id_profiles",
        "endpoints",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_model_configs_profile_id_profiles",
        "model_configs",
        type_="foreignkey",
    )

    op.drop_index(
        "ix_header_blocklist_rules_profile_id", table_name="header_blocklist_rules"
    )
    op.drop_index("ix_audit_logs_profile_id", table_name="audit_logs")
    op.drop_index("ix_request_logs_profile_id", table_name="request_logs")
    op.drop_index(
        "ix_endpoint_fx_rate_settings_profile_id",
        table_name="endpoint_fx_rate_settings",
    )
    op.drop_index("ix_user_settings_profile_id", table_name="user_settings")
    op.drop_index("ix_connections_profile_id", table_name="connections")
    op.drop_index("ix_endpoints_profile_id", table_name="endpoints")
    op.drop_index("ix_model_configs_profile_id", table_name="model_configs")

    op.drop_column("header_blocklist_rules", "profile_id")
    op.drop_column("audit_logs", "profile_id")
    op.drop_column("request_logs", "profile_id")
    op.drop_column("endpoint_fx_rate_settings", "profile_id")
    op.drop_column("user_settings", "profile_id")
    op.drop_column("connections", "profile_id")
    op.drop_column("endpoints", "profile_id")
    op.drop_column("model_configs", "profile_id")

    op.drop_index("uq_profiles_single_active", table_name="profiles")
    op.drop_index("idx_profiles_deleted_at", table_name="profiles")
    op.drop_table("profiles")
