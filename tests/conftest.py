import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_test_postgres_container: Any | None = None
_test_database_url: str | None = None
_DB_FREE_TEST_TARGETS = {
    "tests/services/test_background_tasks.py",
    "tests/test_realtime_broadcast.py",
}
_DB_FREE_DATABASE_URL = "postgresql+asyncpg://prism:prism@localhost:5432/prism_test"


def _to_asyncpg_url(sync_url: str) -> str:
    if sync_url.startswith("postgresql+psycopg2://"):
        return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if sync_url.startswith("postgresql://"):
        return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return sync_url


def _run_alembic_upgrade(database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parent.parent / "alembic"),
    )
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def _build_postgres_container() -> Any:
    postgres_module = importlib.import_module("testcontainers.postgres")
    postgres_container = getattr(postgres_module, "PostgresContainer")
    return postgres_container(
        "postgres:16-alpine",
        username="prism",
        password="prism",
        dbname="prism_test",
    )


def _selected_paths_require_database(session: pytest.Session) -> bool:
    selected_args = [arg for arg in session.config.args if not arg.startswith("-")]
    if not selected_args:
        return True

    normalized_args = {
        str(Path(arg).as_posix()) for arg in selected_args if arg.endswith(".py")
    }
    if not normalized_args:
        return True

    return not normalized_args.issubset(_DB_FREE_TEST_TARGETS)


def pytest_sessionstart(session: pytest.Session) -> None:
    global _test_postgres_container
    global _test_database_url

    os.environ["APP_ENV"] = "test"

    if not _selected_paths_require_database(session):
        os.environ["DATABASE_URL"] = _DB_FREE_DATABASE_URL
        _test_database_url = _DB_FREE_DATABASE_URL
        return

    container = _build_postgres_container()
    container.start()
    sync_url = container.get_connection_url()
    async_url = _to_asyncpg_url(sync_url)
    os.environ["DATABASE_URL"] = async_url
    _run_alembic_upgrade(async_url)

    _test_postgres_container = container
    _test_database_url = async_url


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _test_postgres_container

    if _test_postgres_container is not None:
        _test_postgres_container.stop()
        _test_postgres_container = None


@pytest.fixture(scope="session")
def test_database_url() -> str:
    if _test_database_url is None:
        raise RuntimeError("test database URL was not initialized")
    return _test_database_url


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
