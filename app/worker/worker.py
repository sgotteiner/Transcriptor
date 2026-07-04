"""Async worker process (Stage B).

Drains the shared Redis queue: pops a chunk id, loads its audio and lineage from
Redis, transcribes it (reusing the pipeline's chunk-execution logic, so fallback
and lineage behave identically to sync), and writes the result back. The last
worker to finish a job's chunks assembles the transcript and finalises it.

Run one or many — each process pulls independently from the same queue, so chunks
of a job (and multiple jobs) spread across workers:

    python -m app.worker.worker        # start a worker (repeat for more)

This is the in-process scheduler's Stage B counterpart; in K8s each worker is a
pod replica.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket

from app.backends.factory import BackendRouter
from app.config import get_settings
from app.core.pipeline import Pipeline
from app.logging import configure_logging, get_logger
from app.store.redis_store import RedisQueue, RedisStateStore

log = get_logger("worker")
HOSTNAME = socket.gethostname()


class Worker:
    def __init__(self, settings, store: RedisStateStore, queue: RedisQueue,
                 router: BackendRouter) -> None:
        self._settings = settings
        self._store = store
        self._queue = queue
        self._router = router
        self._pipeline = Pipeline(settings, store, router)  # no scheduler/queue here
        self._stop = asyncio.Event()

    async def process_one(self, chunk_id: str, worker_id: str) -> None:
        chunk = await self._store.get_chunk(chunk_id)
        if chunk is None:
            log.warning("worker.chunk_missing", chunk_id=chunk_id)
            return
        samples = await self._queue.load_audio(chunk_id)
        # Recompute the plan from the chunk's tier/backend — deterministic given
        # the same settings, so no need to ship the plan through the queue.
        plan = self._router.plan(chunk.tier, chunk.backend_requested)
        await self._pipeline._execute(chunk, samples, plan, worker_id)
        await self._queue.discard_audio(chunk_id)
        remaining = await self._queue.decr_remaining(chunk.job_id)
        if remaining <= 0:
            await self._finalize(chunk.job_id)

    async def _finalize(self, job_id: str) -> None:
        job = await self._store.get_job(job_id)
        if job is not None:
            await self._pipeline._finalize(job)  # shared assembly/finalise logic

    async def _consumer(self, idx: int) -> None:
        worker_id = f"{HOSTNAME}/pid-{os.getpid()}/c{idx}"
        while not self._stop.is_set():
            chunk_id = await self._queue.dequeue(timeout=2)
            if chunk_id is None:
                continue
            try:
                await self.process_one(chunk_id, worker_id)
            except Exception as exc:  # pragma: no cover - defensive
                log.error("worker.error", chunk_id=chunk_id, error=str(exc))

    async def run(self) -> None:
        n = max(1, self._settings.max_concurrent_chunks)
        log.info("worker.started", pid=os.getpid(), concurrency=n)
        await asyncio.gather(*[self._consumer(i) for i in range(n)])

    def stop(self) -> None:
        self._stop.set()


async def _amain() -> None:
    settings = get_settings()
    configure_logging()
    if not settings.redis_url:
        raise SystemExit("TRANSCRIPTOR_REDIS_URL must be set to run a worker")

    import redis.asyncio as aioredis

    client = aioredis.Redis.from_url(settings.redis_url)
    worker = Worker(settings, RedisStateStore(client), RedisQueue(client),
                    BackendRouter(settings))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.stop)
        except NotImplementedError:  # Windows has no add_signal_handler
            pass
    try:
        await worker.run()
    finally:
        await worker._pipeline._router.aclose()
        await client.aclose()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
