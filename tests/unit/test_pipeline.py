"""Pipeline end-to-end with a fake backend (no weights): ordering, lineage, modes.

The fake backend deliberately finishes later chunks first, proving the reassembler
restores order regardless of completion order, and that lineage is recorded.
"""

import asyncio

import numpy as np

from app.backends.factory import BackendPlan
from app.config import Settings
from app.core.pipeline import Pipeline
from app.core.scheduler import PriorityScheduler
from app.models import Backend, Chunk, Job, Mode, Status, Tier
from app.store.memory import MemoryStateStore


class FakeBackend:
    """Returns the integer stored in samples[0] as text; later chunks finish first."""

    kind = Backend.LOCAL

    def available(self) -> bool:
        return True

    async def transcribe(self, samples, sample_rate, model_id) -> str:
        index = int(samples[0])
        await asyncio.sleep(0.02 * (5 - index))  # reverse-order completion
        return str(index)


class FakeRouter:
    def __init__(self):
        self._backend = FakeBackend()

    def get(self, backend):
        return self._backend


def _make_work(job: Job, n: int):
    chunks, work = [], []
    for i in range(n):
        c = Chunk(
            job_id=job.job_id, index=i, start_ms=i * 1000, end_ms=(i + 1) * 1000,
            tier=Tier.FAST, backend_requested=Backend.LOCAL,
            priority=0 if job.mode is Mode.SYNC else 1,
        )
        chunks.append(c)
        work.append((c, np.array([i], dtype=np.float32)))
    return chunks, work


async def _pipeline():
    store = MemoryStateStore()
    scheduler = PriorityScheduler(concurrency=3)
    await scheduler.start()
    pipe = Pipeline(Settings(), store, FakeRouter(), scheduler)
    return pipe, store, scheduler


async def test_sync_stream_is_in_order_with_lineage():
    pipe, store, scheduler = await _pipeline()
    try:
        job = Job(mode=Mode.SYNC, tier=Tier.FAST, backend_requested=Backend.LOCAL,
                  source_filename="x")
        await store.create_job(job)
        chunks, work = _make_work(job, 5)
        job.chunk_count = 5
        await store.add_chunks(chunks)

        events = [e async for e in pipe.stream_sync(job, work, BackendPlan(Backend.LOCAL, "m", False))]

        assert events[-1]["type"] == "done"
        assert events[-1]["transcript"] == "0 1 2 3 4"          # order restored
        assert (await store.get_job(job.job_id)).status is Status.DONE

        lineage = await store.list_chunks(job.job_id)
        assert all(c.status is Status.DONE for c in lineage)
        assert all(c.worker_id and c.backend_used is Backend.LOCAL for c in lineage)
        assert all(c.duration_ms is not None for c in lineage)
    finally:
        await scheduler.stop()


async def test_async_job_completes_and_assembles():
    pipe, store, scheduler = await _pipeline()
    try:
        job = Job(mode=Mode.ASYNC, tier=Tier.FAST, backend_requested=Backend.LOCAL,
                  source_filename="x")
        await store.create_job(job)
        chunks, work = _make_work(job, 4)
        job.chunk_count = 4
        await store.add_chunks(chunks)

        await pipe.run_async(job, work, BackendPlan(Backend.LOCAL, "m", False))

        for _ in range(100):  # poll to completion
            if (await store.get_job(job.job_id)).status in (Status.DONE, Status.FAILED):
                break
            await asyncio.sleep(0.02)

        final = await store.get_job(job.job_id)
        assert final.status is Status.DONE
        assert final.transcript == "0 1 2 3"
    finally:
        await scheduler.stop()
