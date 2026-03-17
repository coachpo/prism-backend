import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

BackgroundTaskFn = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class BackgroundTaskJob:
    name: str
    run: BackgroundTaskFn
    max_retries: int = 0
    retry_delay_seconds: float = 0.0


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

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._started:
            return

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
            raise RuntimeError("BackgroundTaskManager has not been started")
        if self._shutting_down:
            raise RuntimeError("BackgroundTaskManager is shutting down")
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

    async def wait_for_idle(self) -> None:
        if self._queue is None:
            return
        await self._queue.join()

    async def shutdown(self) -> None:
        if not self._started or self._queue is None:
            return

        self._shutting_down = True
        await self._queue.join()

        for _ in self._workers:
            self._queue.put_nowait(None)

        await asyncio.gather(*self._workers, return_exceptions=False)

        self._workers.clear()
        self._queue = None
        self._started = False
        self._shutting_down = False

    async def _worker_loop(self, worker_index: int) -> None:
        if self._queue is None:
            return

        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    return
                await self._run_job(job, worker_index)
            finally:
                self._queue.task_done()

    async def _run_job(self, job: BackgroundTaskJob, worker_index: int) -> None:
        attempts = 0

        while True:
            try:
                await job.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                attempts += 1
                if attempts > job.max_retries:
                    logger.exception(
                        "Background task failed: worker=%d name=%s attempts=%d",
                        worker_index,
                        job.name,
                        attempts,
                    )
                    return

                logger.warning(
                    "Retrying background task: worker=%d name=%s attempt=%d/%d",
                    worker_index,
                    job.name,
                    attempts,
                    job.max_retries,
                )

                if job.retry_delay_seconds > 0:
                    await self._sleep_fn(job.retry_delay_seconds)


background_task_manager = BackgroundTaskManager()


__all__ = ["BackgroundTaskJob", "BackgroundTaskManager", "background_task_manager"]
