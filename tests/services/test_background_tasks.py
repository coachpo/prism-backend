import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.main import app, lifespan
from app.services.background_tasks import BackgroundTaskManager


@pytest.mark.asyncio
async def test_background_task_manager_requires_start() -> None:
    manager = BackgroundTaskManager()

    with pytest.raises(RuntimeError, match="has not been started"):
        manager.enqueue(name="test-job", run=AsyncMock())


@pytest.mark.asyncio
async def test_background_task_manager_executes_enqueued_jobs() -> None:
    manager = BackgroundTaskManager()
    event = asyncio.Event()

    async def mark_done() -> None:
        event.set()

    await manager.start()
    manager.enqueue(name="mark-done", run=mark_done)
    await asyncio.wait_for(event.wait(), timeout=1)
    await manager.wait_for_idle()
    await manager.shutdown()


@pytest.mark.asyncio
async def test_background_task_manager_retries_failed_jobs() -> None:
    sleep_mock = AsyncMock()
    manager = BackgroundTaskManager(sleep_fn=sleep_mock)
    attempts = 0
    completed = asyncio.Event()

    async def flaky_job() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("try again")
        completed.set()

    await manager.start()
    manager.enqueue(
        name="flaky-job",
        run=flaky_job,
        max_retries=2,
        retry_delay_seconds=0.25,
    )

    await asyncio.wait_for(completed.wait(), timeout=1)
    await manager.wait_for_idle()
    await manager.shutdown()

    assert attempts == 3
    assert sleep_mock.await_count == 2


@pytest.mark.asyncio
async def test_background_task_manager_shutdown_drains_pending_jobs() -> None:
    manager = BackgroundTaskManager()
    release_first_job = asyncio.Event()
    first_job_started = asyncio.Event()
    second_job_finished = asyncio.Event()

    async def first_job() -> None:
        first_job_started.set()
        await release_first_job.wait()

    async def second_job() -> None:
        second_job_finished.set()

    await manager.start()
    manager.enqueue(name="first-job", run=first_job)
    manager.enqueue(name="second-job", run=second_job)

    await asyncio.wait_for(first_job_started.wait(), timeout=1)
    shutdown_task = asyncio.create_task(manager.shutdown())

    await asyncio.sleep(0)
    assert shutdown_task.done() is False

    release_first_job.set()
    await asyncio.wait_for(shutdown_task, timeout=1)

    assert second_job_finished.is_set() is True
    assert manager.started is False


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_background_task_manager() -> None:
    mock_http_client = SimpleNamespace(aclose=AsyncMock())
    start_mock = AsyncMock()
    shutdown_mock = AsyncMock()
    dispose_mock = AsyncMock()
    mock_engine = SimpleNamespace(dispose=dispose_mock)

    with (
        patch("app.main.bootstrap.run_startup_sequence", AsyncMock()),
        patch("app.main.bootstrap.build_http_client", return_value=mock_http_client),
        patch("app.main.background_task_manager.start", start_mock),
        patch("app.main.background_task_manager.shutdown", shutdown_mock),
        patch("app.main.get_engine", return_value=mock_engine),
    ):
        async with lifespan(app):
            assert app.state.background_task_manager is not None

    start_mock.assert_awaited_once()
    shutdown_mock.assert_awaited_once()
    mock_http_client.aclose.assert_awaited_once()
    dispose_mock.assert_awaited_once()
