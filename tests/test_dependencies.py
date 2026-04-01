from __future__ import annotations

import asyncio
import importlib

import pytest


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
