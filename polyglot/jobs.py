"""In-memory job queue: a fixed number of workers (1 on a Pi 3), one active job per user, cancellation."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_ids = itertools.count(1)


class QueueFull(Exception):
    pass


class AlreadyQueued(Exception):
    pass


@dataclass(eq=False)
class QueuedJob:
    user_id: int
    username: str | None
    chat_id: int
    url: str
    mode: str
    src: str
    dst: str
    cache_key: str
    status_message_id: int | None = None
    id: int = field(default_factory=lambda: next(_ids))
    created: float = field(default_factory=time.monotonic)
    task: asyncio.Task[None] | None = None
    cancelled: bool = False  # by the user
    interrupted: bool = False  # by a bot shutdown: the job stays in the database and resumes on start


Runner = Callable[[QueuedJob], Awaitable[None]]
PositionCallback = Callable[[QueuedJob, int], Awaitable[None]]


class JobQueue:
    def __init__(self, runner: Runner, workers: int, max_queue: int, on_position: PositionCallback | None = None):
        self.runner = runner
        self.workers = max(1, workers)
        self.max_queue = max_queue
        self.on_position = on_position
        self.waiting: list[QueuedJob] = []
        self.running: list[QueuedJob] = []
        self._wakeup = asyncio.Condition()
        self._tasks: list[asyncio.Task[None]] = []
        self._resume = asyncio.Event()
        self._resume.set()

    def active_for(self, user_id: int) -> QueuedJob | None:
        return next((j for j in [*self.running, *self.waiting] if j.user_id == user_id), None)

    async def submit(self, job: QueuedJob) -> int:
        """Queue a job; returns how many jobs are ahead of it (0 = starts right away)."""
        if self.active_for(job.user_id):
            raise AlreadyQueued
        if len(self.waiting) >= self.max_queue:
            raise QueueFull
        async with self._wakeup:
            self.waiting.append(job)
            self._wakeup.notify()
        return self.position(job)

    def position(self, job: QueuedJob) -> int:
        if job in self.running:
            return 0
        free = max(0, self.workers - len(self.running))
        return max(0, self.waiting.index(job) + 1 - free)

    def cancel(self, user_id: int) -> QueuedJob | None:
        job = self.active_for(user_id)
        if not job:
            return None
        job.cancelled = True
        if job in self.waiting:
            self.waiting.remove(job)
        elif job.task:
            job.task.cancel()  # kills its subprocesses (see proc.run)
        return job

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker(i)) for i in range(self.workers)]

    async def stop(self) -> None:
        """Shutdown: running jobs are interrupted (not cancelled), so they can resume after a restart."""
        for job in self.running:
            job.interrupted = True
            if job.task:
                job.task.cancel()
        await asyncio.gather(*(j.task for j in self.running if j.task), return_exceptions=True)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    @contextlib.asynccontextmanager
    async def paused(self) -> AsyncIterator[None]:
        """Maintenance window (e.g. updating yt-dlp): no new job starts, running ones are waited for."""
        self._resume.clear()
        try:
            while self.running:
                await asyncio.sleep(2)
            yield
        finally:
            self._resume.set()
            async with self._wakeup:
                self._wakeup.notify_all()

    async def _worker(self, n: int) -> None:
        while True:
            async with self._wakeup:
                await self._wakeup.wait_for(lambda: bool(self.waiting) and self._resume.is_set())
                job = self.waiting.pop(0)
                self.running.append(job)
            await self._announce_positions()
            job.task = asyncio.create_task(self.runner(job))
            try:
                await job.task
            except asyncio.CancelledError:
                if not (job.cancelled or job.interrupted):
                    raise
                log.info("job %s %s", job.id, "cancelled" if job.cancelled else "interrupted")
            except Exception:
                log.exception("job %s crashed", job.id)
            finally:
                self.running.remove(job)

    async def _announce_positions(self) -> None:
        if not self.on_position:
            return
        for job in list(self.waiting):
            try:
                await self.on_position(job, self.position(job))
            except Exception as err:  # a failed status edit must not stop the worker
                log.debug("position update failed: %s", err)
