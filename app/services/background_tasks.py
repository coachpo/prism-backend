import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

BackgroundTaskFn = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class BackgroundTaskJob:
    name: str
    run: BackgroundTaskFn
    max_retries: int = 0
    retry_delay_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class BackgroundTaskFailureSnapshot:
    attempts: int
    failure_kind: str
    job_kind: str
    error_message: str
    error_type: str
    job_name: str
    occurred_at: datetime
    phase: str
    worker_index: int | None


@dataclass(frozen=True, slots=True)
class BackgroundTaskMetricsSnapshot:
    active_jobs: int
    enqueue_rejections_total: int
    last_failure: BackgroundTaskFailureSnapshot | None
    queue_depth: int
    shutting_down: bool
    started: bool
    total_completed: int
    total_enqueued: int
    retry_attempts_total: int
    terminal_failures_total: int
    worker_count: int


class BackgroundTaskManager:
    def __init__(
        self,
        *,
        worker_count: int = 1,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")

        self._worker_count = worker_count
        self._sleep_fn = sleep_fn
        self._queue: asyncio.Queue[BackgroundTaskJob | None] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._started = False
        self._shutting_down = False
        self._queue_depth = 0
        self._active_jobs = 0
        self._enqueue_rejections_total = 0
        self._total_enqueued = 0
        self._total_completed = 0
        self._retry_attempts_total = 0
        self._terminal_failures_total = 0
        self._last_failure: BackgroundTaskFailureSnapshot | None = None

    def configure(self, *, worker_count: int) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be at least 1")
        if self._started:
            if worker_count == self._worker_count:
                return
            raise RuntimeError(
                "BackgroundTaskManager cannot be reconfigured after start"
            )

        self._worker_count = worker_count

    @property
    def started(self) -> bool:
        return self._started

    @property
    def metrics(self) -> BackgroundTaskMetricsSnapshot:
        return BackgroundTaskMetricsSnapshot(
            active_jobs=self._active_jobs,
            enqueue_rejections_total=self._enqueue_rejections_total,
            last_failure=self._last_failure,
            queue_depth=self._queue_depth,
            shutting_down=self._shutting_down,
            started=self._started,
            total_completed=self._total_completed,
            total_enqueued=self._total_enqueued,
            retry_attempts_total=self._retry_attempts_total,
            terminal_failures_total=self._terminal_failures_total,
            worker_count=self._worker_count,
        )

    def snapshot(self) -> BackgroundTaskMetricsSnapshot:
        return self.metrics

    async def start(self) -> None:
        if self._started:
            return

        self._reset_metrics()
        self._queue = asyncio.Queue()
        self._workers = [
            asyncio.create_task(
                self._worker_loop(worker_index),
                name=f"background-task-worker-{worker_index}",
            )
            for worker_index in range(self._worker_count)
        ]
        self._started = True
        self._shutting_down = False

    def enqueue(
        self,
        *,
        name: str,
        run: BackgroundTaskFn,
        max_retries: int = 0,
        retry_delay_seconds: float = 0.0,
    ) -> None:
        if not self._started or self._queue is None:
            error = RuntimeError("BackgroundTaskManager has not been started")
            self._record_enqueue_rejection(
                name=name,
                error=error,
                failure_kind="not_started",
            )
            raise error
        if self._shutting_down and not self._is_current_worker_task():
            error = RuntimeError("BackgroundTaskManager is shutting down")
            self._record_enqueue_rejection(
                name=name,
                error=error,
                failure_kind="shutting_down",
            )
            raise error
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds cannot be negative")

        self._queue.put_nowait(
            BackgroundTaskJob(
                name=name,
                run=run,
                max_retries=max_retries,
                retry_delay_seconds=retry_delay_seconds,
            )
        )
        self._queue_depth += 1
        self._total_enqueued += 1

    async def wait_for_idle(self) -> None:
        if self._queue is None:
            return
        await self._queue.join()

    async def shutdown(self) -> None:
        if not self._started or self._queue is None:
            return

        queue = self._queue
        workers = list(self._workers)
        self._shutting_down = True
        try:
            await queue.join()

            for _ in workers:
                queue.put_nowait(None)

            await asyncio.gather(*workers, return_exceptions=False)
        finally:
            self._workers.clear()
            self._queue = None
            self._started = False
            self._shutting_down = False
            self._reset_live_gauges()

    async def _worker_loop(self, worker_index: int) -> None:
        if self._queue is None:
            return

        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    return
                self._active_jobs += 1
                try:
                    await self._run_job(job, worker_index)
                finally:
                    self._active_jobs = max(0, self._active_jobs - 1)
                    self._queue_depth = max(0, self._queue_depth - 1)
            finally:
                self._queue.task_done()

    async def _run_job(self, job: BackgroundTaskJob, worker_index: int) -> None:
        attempts = 0

        while True:
            try:
                await job.run()
                self._total_completed += 1
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempts += 1
                if attempts > job.max_retries:
                    error = BackgroundTaskFailureSnapshot(
                        attempts=attempts,
                        failure_kind="job_exception",
                        job_kind=self._job_kind(job.name),
                        error_message=str(exc),
                        error_type=type(exc).__name__,
                        job_name=job.name,
                        occurred_at=datetime.now(UTC),
                        phase="run",
                        worker_index=worker_index,
                    )
                    self._last_failure = error
                    self._terminal_failures_total += 1
                    logger.exception(
                        "Background task failed: worker=%d name=%s attempts=%d",
                        worker_index,
                        job.name,
                        attempts,
                    )
                    return

                self._retry_attempts_total += 1
                logger.warning(
                    "Retrying background task: worker=%d name=%s attempt=%d/%d",
                    worker_index,
                    job.name,
                    attempts,
                    job.max_retries,
                )

                if job.retry_delay_seconds > 0:
                    await self._sleep_fn(job.retry_delay_seconds)

    def _job_kind(self, job_name: str) -> str:
        prefix, _, _ = job_name.partition(":")
        return prefix or job_name

    def _is_current_worker_task(self) -> bool:
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            return False

        if current_task is None:
            return False

        return any(current_task is worker for worker in self._workers)

    def _record_enqueue_rejection(
        self,
        *,
        name: str,
        error: RuntimeError,
        failure_kind: str,
    ) -> None:
        self._enqueue_rejections_total += 1
        self._last_failure = BackgroundTaskFailureSnapshot(
            attempts=0,
            failure_kind=failure_kind,
            job_kind=self._job_kind(name),
            error_message=str(error),
            error_type=type(error).__name__,
            job_name=name,
            occurred_at=datetime.now(UTC),
            phase="enqueue",
            worker_index=None,
        )

    def _reset_metrics(self) -> None:
        self._queue_depth = 0
        self._active_jobs = 0
        self._enqueue_rejections_total = 0
        self._total_enqueued = 0
        self._total_completed = 0
        self._retry_attempts_total = 0
        self._terminal_failures_total = 0
        self._last_failure = None

    def _reset_live_gauges(self) -> None:
        self._queue_depth = 0
        self._active_jobs = 0


background_task_manager = BackgroundTaskManager()


__all__ = [
    "BackgroundTaskFailureSnapshot",
    "BackgroundTaskJob",
    "BackgroundTaskManager",
    "BackgroundTaskMetricsSnapshot",
    "background_task_manager",
]
