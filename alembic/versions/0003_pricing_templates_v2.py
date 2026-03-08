"""Introduce profile-scoped pricing templates and connection references.

Compatibility migration for databases created before the squashed baseline.
Fresh databases already have the target schema in ``0001_initial``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op


revision = "0003_pricing_templates_v2"
down_revision = "0002_utc_timestamps"
branch_labels = None
depends_on = None


LEGACY_CONNECTION_COLUMNS: tuple[str, ...] = (
    "pricing_enabled",
    "pricing_currency_code",
    "input_price",
    "output_price",
    "cached_input_price",
    "cache_creation_price",
    "reasoning_price",
    "missing_special_token_price_policy",
    "pricing_config_version",
)


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _foreign_key_exists(
    table_name: str,
    *,
    constrained_columns: list[str],
    referred_table: str,
) -> bool:
    inspector = sa.inspect(op.get_bind())
    for foreign_key in inspector.get_foreign_keys(table_name):
        if foreign_key.get("referred_table") != referred_table:
            continue
        if foreign_key.get("constrained_columns") == constrained_columns:
            return True
    return False


def _parse_optional_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _parse_required_decimal(value: str | None) -> Decimal | None:
    return _parse_optional_decimal(value)


def _is_effective_legacy_pricing(row: sa.Row) -> bool:
    if not row.pricing_enabled:
        return False
    if row.pricing_currency_code is None:
        return False
    input_price = _parse_required_decimal(row.input_price)
    output_price = _parse_required_decimal(row.output_price)
    if input_price is None or output_price is None:
        return False
    if (
        row.cached_input_price is not None
        and _parse_optional_decimal(row.cached_input_price) is None
    ):
        return False
    if (
        row.cache_creation_price is not None
        and _parse_optional_decimal(row.cache_creation_price) is None
    ):
        return False
    if (
        row.reasoning_price is not None
        and _parse_optional_decimal(row.reasoning_price) is None
    ):
        return False
    return True


def _create_pricing_templates_table() -> None:
    op.create_table(
        "pricing_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "pricing_unit",
            sa.String(length=20),
            nullable=False,
            server_default="PER_1M",
        ),
        sa.Column("pricing_currency_code", sa.String(length=3), nullable=False),
        sa.Column("input_price", sa.String(length=20), nullable=False),
        sa.Column("output_price", sa.String(length=20), nullable=False),
        sa.Column("cached_input_price", sa.String(length=20), nullable=True),
        sa.Column("cache_creation_price", sa.String(length=20), nullable=True),
        sa.Column("reasoning_price", sa.String(length=20), nullable=True),
        sa.Column(
            "missing_special_token_price_policy",
            sa.String(length=20),
            nullable=False,
            server_default="MAP_TO_OUTPUT",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('utc', now())"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('utc', now())"),
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "name", name="uq_pricing_templates_profile_name"
        ),
    )


def _migrate_legacy_pricing_rows() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT
                id,
                profile_id,
                pricing_enabled,
                pricing_currency_code,
                input_price,
                output_price,
                cached_input_price,
                cache_creation_price,
                reasoning_price,
                missing_special_token_price_policy,
                pricing_config_version
            FROM connections
            ORDER BY profile_id ASC, id ASC
            """
        )
    ).fetchall()

    groups: dict[tuple, dict[str, object]] = {}
    profile_name_counters: dict[int, int] = {}
    connection_to_template_key: dict[int, tuple] = {}

    for row in rows:
        if not _is_effective_legacy_pricing(row):
            continue

        key = (
            row.profile_id,
            row.pricing_currency_code,
            row.input_price,
            row.output_price,
            row.cached_input_price,
            row.cache_creation_price,
            row.reasoning_price,
            row.missing_special_token_price_policy or "MAP_TO_OUTPUT",
        )

        group = groups.get(key)
        if group is None:
            profile_counter = profile_name_counters.get(row.profile_id, 0) + 1
            profile_name_counters[row.profile_id] = profile_counter
            group = {
                "profile_id": row.profile_id,
                "name": f"Migrated Pricing Template {profile_counter}",
                "pricing_currency_code": row.pricing_currency_code,
                "input_price": row.input_price,
                "output_price": row.output_price,
                "cached_input_price": row.cached_input_price,
                "cache_creation_price": row.cache_creation_price,
                "reasoning_price": row.reasoning_price,
                "missing_special_token_price_policy": row.missing_special_token_price_policy
                or "MAP_TO_OUTPUT",
                "max_config_version": row.pricing_config_version or 1,
            }
            groups[key] = group
        else:
            max_version = int(group["max_config_version"])
            group["max_config_version"] = max(
                max_version, row.pricing_config_version or 1
            )

        connection_to_template_key[row.id] = key

    template_ids_by_key: dict[tuple, int] = {}
    for key, group in groups.items():
        insert_result = bind.execute(
            sa.text(
                """
                INSERT INTO pricing_templates (
                    profile_id,
                    name,
                    description,
                    pricing_unit,
                    pricing_currency_code,
                    input_price,
                    output_price,
                    cached_input_price,
                    cache_creation_price,
                    reasoning_price,
                    missing_special_token_price_policy,
                    version
                )
                VALUES (
                    :profile_id,
                    :name,
                    NULL,
                    'PER_1M',
                    :pricing_currency_code,
                    :input_price,
                    :output_price,
                    :cached_input_price,
                    :cache_creation_price,
                    :reasoning_price,
                    :missing_special_token_price_policy,
                    :version
                )
                RETURNING id
                """
            ),
            {
                "profile_id": group["profile_id"],
                "name": group["name"],
                "pricing_currency_code": group["pricing_currency_code"],
                "input_price": group["input_price"],
                "output_price": group["output_price"],
                "cached_input_price": group["cached_input_price"],
                "cache_creation_price": group["cache_creation_price"],
                "reasoning_price": group["reasoning_price"],
                "missing_special_token_price_policy": group[
                    "missing_special_token_price_policy"
                ],
                "version": int(group["max_config_version"] or 1),
            },
        )
        template_ids_by_key[key] = int(insert_result.scalar_one())

    for connection_id, key in connection_to_template_key.items():
        bind.execute(
            sa.text(
                "UPDATE connections SET pricing_template_id = :template_id WHERE id = :connection_id"
            ),
            {
                "template_id": template_ids_by_key[key],
                "connection_id": connection_id,
            },
        )


def upgrade() -> None:
    if not _table_exists("pricing_templates"):
        _create_pricing_templates_table()

    if not _index_exists("pricing_templates", "ix_pricing_templates_profile_id"):
        op.create_index(
            "ix_pricing_templates_profile_id",
            "pricing_templates",
            ["profile_id"],
            unique=False,
        )
    if not _index_exists("pricing_templates", "idx_pricing_templates_profile_id"):
        op.create_index(
            "idx_pricing_templates_profile_id",
            "pricing_templates",
            ["profile_id"],
            unique=False,
        )

    if not _column_exists("connections", "pricing_template_id"):
        op.add_column(
            "connections",
            sa.Column("pricing_template_id", sa.Integer(), nullable=True),
        )

    if all(_column_exists("connections", column) for column in LEGACY_CONNECTION_COLUMNS):
        _migrate_legacy_pricing_rows()

    if not _index_exists("connections", "idx_connections_pricing_template_id"):
        op.create_index(
            "idx_connections_pricing_template_id",
            "connections",
            ["pricing_template_id"],
            unique=False,
        )
    if not _foreign_key_exists(
        "connections",
        constrained_columns=["pricing_template_id"],
        referred_table="pricing_templates",
    ):
        op.create_foreign_key(
            "fk_connections_pricing_template_id",
            "connections",
            "pricing_templates",
            ["pricing_template_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    for column_name in LEGACY_CONNECTION_COLUMNS:
        if _column_exists("connections", column_name):
            op.drop_column("connections", column_name)


def downgrade() -> None:
    pass
