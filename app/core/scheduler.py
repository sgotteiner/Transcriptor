"""In-process priority scheduler — the Stage A stand-in for a queue + worker pool.

A fixed number of consumer tasks (= MAX_CONCURRENT_CHUNKS) pull work from a
priority queue. That fixed count *is* the admission cap; the priority ordering is
the lane policy (sync before async, then job arrival, then chunk index). In Stage B
this is replaced by a Redis queue drained by separate worker pods — same contract,
shared across machines.

The scheduler is deliberately generic: callers submit a coroutine factory that
receives the assigned ``worker_id``; all backend/lineage logic lives in the
pipeline. That keeps this component reusable and easy to test.
"""

from __future__ import annotations

import asyncio
import itertools
import socket
from typing import Awaitable, Callable

from app.logging import get_logger

log = get_logger("scheduler")

HOSTNAME = socket.gethostname()

# A factory that, given the worker id it runs on, does the work and returns text.
WorkFactory = Callable[[str], Awaitable[str]]


class PriorityScheduler:
    def __init__(self, concurrency: int) -> None:
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._workers:
            return
        for i in range(self._concurrency):
            worker_id = f"{HOSTNAME}/worker-{i}"
            self._workers.append(asyncio.create_task(self._run(worker_id)))
        log.info("scheduler.started", concurrency=self._concurrency)

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()

    def submit(self, priority: tuple[int, ...], factory: WorkFactory) -> asyncio.Future:
        """Enqueue work. Returns a future resolving to the factory's result."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        # seq is unique and monotonic, so equal priorities break deterministically
        # by submission order and the queue never compares the payload objects.
        self._queue.put_nowait((priority, next(self._seq), factory, fut))
        return fut

    async def _run(self, worker_id: str) -> None:
        while True:
            _priority, _seq, factory, fut = await self._queue.get()
            try:
                if not fut.cancelled():
                    result = await factory(worker_id)
                    if not fut.cancelled():
                        fut.set_result(result)
            except Exception as exc:  # pragma: no cover - defensive
                if not fut.cancelled():
                    fut.set_exception(exc)
            finally:
                self._queue.task_done()
