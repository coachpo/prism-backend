from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        pytest.fail(
            f"{module_name} must exist for monitoring probe runner tests: {exc}"
        )


class _RecoverySession:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class TestMonitoringProbeRunner:
    @pytest.mark.asyncio
    async def test_run_connection_probe_releases_probe_lease_with_recovery_session_after_probe_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        probe_runner_module = _load_module("app.services.monitoring.probe_runner")
        run_connection_probe = getattr(
            probe_runner_module, "run_connection_probe", None
        )
        assert run_connection_probe is not None, (
            "app.services.monitoring.probe_runner.run_connection_probe must exist"
        )

        primary_session = SimpleNamespace(poisoned=False)
        recovery_sessions: list[_RecoverySession] = []
        released_sessions: list[object] = []

        def fake_session_factory() -> _RecoverySession:
            session = _RecoverySession()
            recovery_sessions.append(session)
            return session

        monkeypatch.setattr(
            probe_runner_module, "AsyncSessionLocal", fake_session_factory
        )

        connection = SimpleNamespace(
            id=11,
            endpoint_rel=SimpleNamespace(id=22, base_url="https://api.example.com/v1"),
            model_config_rel=SimpleNamespace(
                id=33,
                api_family="openai",
                model_id="gpt-5.4",
                vendor=SimpleNamespace(id=44),
            ),
        )

        async def load_connection_fn(db, *, profile_id: int, connection_id: int):
            assert db is primary_session
            assert profile_id == 7
            assert connection_id == 11
            return connection

        async def load_blocklist_rules_fn(db, *, profile_id: int):
            assert db is primary_session
            assert profile_id == 7
            return []

        def build_upstream_headers_fn(connection_obj, api_family: str, **kwargs):
            assert connection_obj is connection
            assert api_family == "openai"
            _ = kwargs
            return {"authorization": "Bearer test"}

        async def execute_probe_request_fn(*args, **kwargs):
            _ = args
            _ = kwargs
            return ("healthy", "Connection successful", 25)

        async def acquire_probe_lease_fn(**kwargs):
            assert kwargs["session"] is primary_session
            return SimpleNamespace(admitted=True, lease_token="lease-token")

        async def record_probe_outcome_fn(**kwargs):
            assert kwargs["session"] is primary_session
            primary_session.poisoned = True
            raise RuntimeError("probe persistence failed")

        async def release_probe_lease_fn(
            *, session, profile_id: int, lease_token: str, now_at
        ):
            released_sessions.append(session)
            assert profile_id == 7
            assert lease_token == "lease-token"
            assert now_at is not None
            if session is primary_session:
                raise AssertionError(
                    "lease release must not reuse the poisoned primary session"
                )
            return True

        with pytest.raises(RuntimeError, match="probe persistence failed"):
            await run_connection_probe(
                db=primary_session,
                client=SimpleNamespace(),
                profile_id=7,
                connection_id=11,
                checked_at=None,
                acquire_probe_lease=True,
                load_connection_fn=load_connection_fn,
                load_blocklist_rules_fn=load_blocklist_rules_fn,
                build_upstream_headers_fn=build_upstream_headers_fn,
                execute_probe_request_fn=execute_probe_request_fn,
                acquire_probe_lease_fn=acquire_probe_lease_fn,
                release_probe_lease_fn=release_probe_lease_fn,
                record_probe_outcome_fn=record_probe_outcome_fn,
                resolve_probe_jitter_seconds_fn=lambda: 0.0,
                sleep_fn=lambda *_args, **_kwargs: None,
            )

        assert len(recovery_sessions) == 1
        assert released_sessions == recovery_sessions
        assert recovery_sessions[0].committed is True
