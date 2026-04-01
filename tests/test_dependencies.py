from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        pytest.fail(f"{module_name} must exist for dependency tests: {exc}")


class _FailingRollbackSession:
    def __init__(self) -> None:
        self.commit_calls = 0
        self.rollback_calls = 0

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1
        raise RuntimeError("rollback failed because connection is closed")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class TestGetDb:
    @pytest.mark.asyncio
    async def test_get_db_preserves_original_exception_when_rollback_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        dependencies_module = _load_module("app.dependencies")
        get_db = getattr(dependencies_module, "get_db", None)
        assert get_db is not None, "app.dependencies.get_db must exist"

        session = _FailingRollbackSession()
        monkeypatch.setattr(dependencies_module, "AsyncSessionLocal", lambda: session)

        generator = get_db()
        yielded_session = await generator.__anext__()
        assert yielded_session is session

        with pytest.raises(ValueError, match="request failed"):
            await generator.athrow(ValueError("request failed"))

        assert session.commit_calls == 0
        assert session.rollback_calls == 1

    @pytest.mark.asyncio
    async def test_get_db_preserves_cancelled_error_when_rollback_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        dependencies_module = _load_module("app.dependencies")
        get_db = getattr(dependencies_module, "get_db", None)
        assert get_db is not None, "app.dependencies.get_db must exist"

        session = _FailingRollbackSession()
        monkeypatch.setattr(dependencies_module, "AsyncSessionLocal", lambda: session)

        generator = get_db()
        yielded_session = await generator.__anext__()
        assert yielded_session is session

        with pytest.raises(asyncio.CancelledError):
            await generator.athrow(asyncio.CancelledError())

        assert session.commit_calls == 0
        assert session.rollback_calls == 1

    @pytest.mark.asyncio
    async def test_import_config_preserves_original_db_error_when_rollback_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        dependencies_module = _load_module("app.dependencies")
        import_export_module = _load_module("app.routers.config_domains.import_export")
        get_db = getattr(dependencies_module, "get_db", None)
        import_config = getattr(import_export_module, "import_config", None)
        assert get_db is not None, "app.dependencies.get_db must exist"
        assert import_config is not None, (
            "app.routers.config_domains.import_export.import_config must exist"
        )

        session = _FailingRollbackSession()
        monkeypatch.setattr(dependencies_module, "AsyncSessionLocal", lambda: session)
        monkeypatch.setattr(
            import_export_module, "validate_import_payload", lambda _data: None
        )

        async def fake_execute_import_payload(db, *, profile_id: int, data):
            assert db is session
            assert profile_id == 1
            assert data.version == 1
            raise OperationalError(
                "DELETE FROM routing_connection_runtime_state",
                {"profile_id": profile_id},
                RuntimeError("db failed during import"),
            )

        monkeypatch.setattr(
            import_export_module,
            "execute_import_payload",
            fake_execute_import_payload,
        )

        generator = get_db()
        yielded_session = await generator.__anext__()
        assert yielded_session is session

        payload = SimpleNamespace(version=1)

        with pytest.raises(OperationalError, match="db failed during import"):
            try:
                await import_config(data=payload, db=yielded_session, profile_id=1)
            except Exception as exc:
                await generator.athrow(exc)

        assert session.commit_calls == 0
        assert session.rollback_calls == 1
