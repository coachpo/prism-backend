from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def _build_alembic_config(database_url: str) -> Config:
    package_root = Path(__file__).resolve().parents[1]
    config = Config(str(package_root / "alembic.ini"))
    migrations_dir = package_root / "alembic"
    config.set_main_option("script_location", str(migrations_dir))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def run_migrations(database_url: str) -> None:
    config = _build_alembic_config(database_url)
    command.upgrade(config, "head")
