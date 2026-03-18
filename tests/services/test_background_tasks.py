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

    metrics = manager.metrics

    assert metrics.enqueue_rejections_total == 1
    assert metrics.last_failure is not None
    assert metrics.last_failure.phase == "enqueue"
    assert metrics.last_failure.failure_kind == "not_started"
    assert metrics.last_failure.job_name == "test-job"


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

    metrics = manager.metrics

    await manager.shutdown()

    assert metrics.total_enqueued == 1
    assert metrics.total_completed == 1
    assert metrics.retry_attempts_total == 0
    assert metrics.terminal_failures_total == 0
    assert metrics.enqueue_rejections_total == 0
    assert metrics.queue_depth == 0
    assert metrics.active_jobs == 0
    assert metrics.last_failure is None


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

    metrics = manager.metrics

    await manager.shutdown()

    assert attempts == 3
    assert sleep_mock.await_count == 2
    assert metrics.total_enqueued == 1
    assert metrics.total_completed == 1
    assert metrics.retry_attempts_total == 2
    assert metrics.terminal_failures_total == 0
    assert metrics.enqueue_rejections_total == 0
    assert metrics.last_failure is None


@pytest.mark.asyncio
async def test_background_task_manager_metrics_track_queue_depth_and_active_jobs() -> (
    None
):
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

    mid_metrics = manager.metrics

    release_first_job.set()
    await asyncio.wait_for(second_job_finished.wait(), timeout=1)
    await manager.wait_for_idle()

    final_metrics = manager.metrics

    await manager.shutdown()

    assert mid_metrics.started is True
    assert mid_metrics.queue_depth == 2
    assert mid_metrics.active_jobs == 1
    assert mid_metrics.total_enqueued == 2
    assert mid_metrics.total_completed == 0
    assert mid_metrics.terminal_failures_total == 0
    assert mid_metrics.retry_attempts_total == 0
    assert mid_metrics.enqueue_rejections_total == 0
    assert final_metrics.queue_depth == 0
    assert final_metrics.active_jobs == 0
    assert final_metrics.total_completed == 2
    assert final_metrics.terminal_failures_total == 0


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
async def test_background_task_manager_rejects_enqueue_during_shutdown() -> None:
    manager = BackgroundTaskManager()
    release_job = asyncio.Event()
    job_started = asyncio.Event()

    async def blocking_job() -> None:
        job_started.set()
        await release_job.wait()

    await manager.start()
    manager.enqueue(name="blocking-job", run=blocking_job)

    await asyncio.wait_for(job_started.wait(), timeout=1)
    shutdown_task = asyncio.create_task(manager.shutdown())

    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="is shutting down"):
        manager.enqueue(name="late-job", run=AsyncMock())

    metrics = manager.metrics

    release_job.set()
    await asyncio.wait_for(shutdown_task, timeout=1)

    assert metrics.enqueue_rejections_total == 1
    assert metrics.last_failure is not None
    assert metrics.last_failure.phase == "enqueue"
    assert metrics.last_failure.failure_kind == "shutting_down"
    assert metrics.last_failure.job_name == "late-job"


@pytest.mark.asyncio
async def test_background_task_manager_allows_worker_follow_up_enqueue_during_shutdown() -> (
    None
):
    manager = BackgroundTaskManager()
    release_first_job = asyncio.Event()
    first_job_started = asyncio.Event()
    follow_up_finished = asyncio.Event()

    async def follow_up_job() -> None:
        follow_up_finished.set()

    async def first_job() -> None:
        first_job_started.set()
        await release_first_job.wait()
        manager.enqueue(name="follow-up-job", run=follow_up_job)

    await manager.start()
    manager.enqueue(name="first-job", run=first_job)

    await asyncio.wait_for(first_job_started.wait(), timeout=1)
    shutdown_task = asyncio.create_task(manager.shutdown())

    await asyncio.sleep(0)
    release_first_job.set()
    await asyncio.wait_for(shutdown_task, timeout=1)

    metrics = manager.metrics

    assert follow_up_finished.is_set() is True
    assert metrics.total_enqueued == 2
    assert metrics.total_completed == 2
    assert metrics.enqueue_rejections_total == 0


@pytest.mark.asyncio
async def test_background_task_manager_continues_after_permanent_failure() -> None:
    sleep_mock = AsyncMock()
    manager = BackgroundTaskManager(sleep_fn=sleep_mock)
    attempts = 0
    follow_up_finished = asyncio.Event()

    async def always_fail() -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("still failing")

    async def follow_up_job() -> None:
        follow_up_finished.set()

    await manager.start()
    manager.enqueue(
        name="always-fail",
        run=always_fail,
        max_retries=2,
        retry_delay_seconds=0.25,
    )
    manager.enqueue(name="follow-up", run=follow_up_job)

    await asyncio.wait_for(follow_up_finished.wait(), timeout=1)
    await manager.wait_for_idle()

    metrics = manager.metrics

    await manager.shutdown()

    assert attempts == 3
    assert sleep_mock.await_count == 2
    assert metrics.total_enqueued == 2
    assert metrics.total_completed == 1
    assert metrics.retry_attempts_total == 2
    assert metrics.terminal_failures_total == 1
    assert metrics.enqueue_rejections_total == 0
    assert metrics.last_failure is not None
    assert metrics.last_failure.job_name == "always-fail"
    assert metrics.last_failure.job_kind == "always-fail"
    assert metrics.last_failure.attempts == 3
    assert metrics.last_failure.phase == "run"
    assert metrics.last_failure.failure_kind == "job_exception"
    assert metrics.last_failure.error_type == "RuntimeError"
    assert metrics.last_failure.error_message == "still failing"


@pytest.mark.asyncio
async def test_background_task_manager_start_resets_metrics() -> None:
    manager = BackgroundTaskManager()

    async def fail_job() -> None:
        raise RuntimeError("boom once")

    await manager.start()
    manager.enqueue(name="failing-job", run=fail_job)
    await manager.wait_for_idle()
    await manager.shutdown()

    failed_metrics = manager.metrics

    await manager.start()
    reset_metrics = manager.metrics
    await manager.shutdown()

    assert failed_metrics.terminal_failures_total == 1
    assert failed_metrics.last_failure is not None
    assert reset_metrics.started is True
    assert reset_metrics.queue_depth == 0
    assert reset_metrics.active_jobs == 0
    assert reset_metrics.total_enqueued == 0
    assert reset_metrics.total_completed == 0
    assert reset_metrics.retry_attempts_total == 0
    assert reset_metrics.terminal_failures_total == 0
    assert reset_metrics.enqueue_rejections_total == 0
    assert reset_metrics.last_failure is None


@pytest.mark.asyncio
async def test_background_task_manager_shutdown_resets_state_when_gather_fails() -> (
    None
):
    manager = BackgroundTaskManager()

    async def blocking_worker() -> None:
        await asyncio.sleep(3600)

    worker = asyncio.create_task(blocking_worker())
    manager._queue = asyncio.Queue()
    manager._workers = [worker]
    manager._started = True

    with patch(
        "app.services.background_tasks.asyncio.gather",
        AsyncMock(side_effect=RuntimeError("worker gather failed")),
    ):
        with pytest.raises(RuntimeError, match="worker gather failed"):
            await manager.shutdown()

    assert manager.started is False
    assert manager._queue is None
    assert manager._workers == []
    assert manager._shutting_down is False
    assert manager.metrics.queue_depth == 0
    assert manager.metrics.active_jobs == 0

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


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
            assert app.state.http_client is mock_http_client
            assert app.state.background_task_manager is not None

    assert app.state.background_task_manager is None
    assert app.state.http_client is None
    start_mock.assert_awaited_once()
    shutdown_mock.assert_awaited_once()
    mock_http_client.aclose.assert_awaited_once()
    dispose_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_cleans_up_http_client_when_worker_start_fails() -> None:
    mock_http_client = SimpleNamespace(aclose=AsyncMock())
    dispose_mock = AsyncMock()
    mock_engine = SimpleNamespace(dispose=dispose_mock)

    with (
        patch("app.main.bootstrap.run_startup_sequence", AsyncMock()),
        patch("app.main.bootstrap.build_http_client", return_value=mock_http_client),
        patch(
            "app.main.background_task_manager.start",
            AsyncMock(side_effect=RuntimeError("worker start failed")),
        ),
        patch("app.main.get_engine", return_value=mock_engine),
    ):
        with pytest.raises(RuntimeError, match="worker start failed"):
            async with lifespan(app):
                pass

    assert app.state.background_task_manager is None
    assert app.state.http_client is None
    mock_http_client.aclose.assert_awaited_once()
    dispose_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_cleans_up_resources_when_worker_shutdown_fails() -> None:
    mock_http_client = SimpleNamespace(aclose=AsyncMock())
    start_mock = AsyncMock()
    shutdown_mock = AsyncMock(side_effect=RuntimeError("worker shutdown failed"))
    dispose_mock = AsyncMock()
    mock_engine = SimpleNamespace(dispose=dispose_mock)

    with (
        patch("app.main.bootstrap.run_startup_sequence", AsyncMock()),
        patch("app.main.bootstrap.build_http_client", return_value=mock_http_client),
        patch("app.main.background_task_manager.start", start_mock),
        patch("app.main.background_task_manager.shutdown", shutdown_mock),
        patch("app.main.get_engine", return_value=mock_engine),
    ):
        with pytest.raises(RuntimeError, match="worker shutdown failed"):
            async with lifespan(app):
                assert app.state.http_client is mock_http_client
                assert app.state.background_task_manager is not None

    assert app.state.background_task_manager is None
    assert app.state.http_client is None
    start_mock.assert_awaited_once()
    shutdown_mock.assert_awaited_once()
    mock_http_client.aclose.assert_awaited_once()
    dispose_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_clears_state_when_startup_sequence_fails() -> None:
    dispose_mock = AsyncMock()
    mock_engine = SimpleNamespace(dispose=dispose_mock)
    app.state.background_task_manager = object()
    app.state.http_client = object()

    with (
        patch(
            "app.main.bootstrap.run_startup_sequence",
            AsyncMock(side_effect=RuntimeError("startup failed")),
        ),
        patch("app.main.get_engine", return_value=mock_engine),
    ):
        with pytest.raises(RuntimeError, match="startup failed"):
            async with lifespan(app):
                pass

    assert app.state.background_task_manager is None
    assert app.state.http_client is None
    dispose_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_clears_state_when_http_client_build_fails() -> None:
    dispose_mock = AsyncMock()
    mock_engine = SimpleNamespace(dispose=dispose_mock)
    app.state.background_task_manager = object()
    app.state.http_client = object()

    with (
        patch("app.main.bootstrap.run_startup_sequence", AsyncMock()),
        patch(
            "app.main.bootstrap.build_http_client",
            side_effect=RuntimeError("http client failed"),
        ),
        patch("app.main.get_engine", return_value=mock_engine),
    ):
        with pytest.raises(RuntimeError, match="http client failed"):
            async with lifespan(app):
                pass

    assert app.state.background_task_manager is None
    assert app.state.http_client is None
    dispose_mock.assert_awaited_once()
