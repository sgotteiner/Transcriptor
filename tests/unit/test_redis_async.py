"""Stage B async-through-Redis, verified with fakeredis (no server, no model).

A shared FakeServer lets an 'API' client and a separate 'worker' client share
state — simulating two processes on one Redis. The backend is mocked, so this
tests the Redis store + queue + worker wiring and job finalisation, not inference.
"""

import fakeredis.aioredis
import numpy as np

from app.backends.factory import BackendPlan
from app.config import Settings
from app.core.pipeline import Pipeline
from app.models import Backend, Chunk, Job, Mode, Status, Tier
from app.store.redis_store import RedisQueue, RedisStateStore
from app.worker.worker import Worker


class FakeBackend:
    kind = Backend.LOCAL

    def available(self) -> bool:
        return True

    async def transcribe(self, samples, sample_rate, model_id) -> str:
        return str(int(samples[0]))  # echo the index we encoded into the audio


class FakeRouter:
    def __init__(self):
        self._backend = FakeBackend()

    def get(self, backend):
        return self._backend

    def plan(self, tier, requested):
        return BackendPlan(Backend.LOCAL, "m", allow_local_fallback=False)


def _make(job, n):
    chunks, work = [], []
    for i in range(n):
        c = Chunk(job_id=job.job_id, index=i, start_ms=i * 1000, end_ms=(i + 1) * 1000,
                  tier=Tier.FAST, backend_requested=Backend.LOCAL, priority=1)
        chunks.append(c)
        work.append((c, np.array([i], dtype=np.float32)))
    return chunks, work


async def test_async_through_redis_finalises_with_transcript():
    server = fakeredis.FakeServer()
    api_r = fakeredis.aioredis.FakeRedis(server=server)
    worker_r = fakeredis.aioredis.FakeRedis(server=server)  # a separate "process"
    settings = Settings(max_concurrent_chunks=2)
    router = FakeRouter()

    # --- API side: persist job + chunks, enqueue for workers ---------------
    api_store, api_queue = RedisStateStore(api_r), RedisQueue(api_r)
    pipeline = Pipeline(settings, api_store, router, scheduler=None, async_queue=api_queue)

    job = Job(mode=Mode.ASYNC, tier=Tier.FAST, backend_requested=Backend.LOCAL,
              source_filename="x")
    await api_store.create_job(job)
    chunks, work = _make(job, 4)
    job.chunk_count = 4
    await api_store.add_chunks(chunks)
    await api_store.save_job(job)
    await pipeline.run_async(job, work, plan=None)  # enqueues to Redis

    # --- Worker side: drain the shared queue --------------------------------
    worker_store, worker_queue = RedisStateStore(worker_r), RedisQueue(worker_r)
    worker = Worker(settings, worker_store, worker_queue, router)
    for _ in range(20):
        chunk_id = await worker_queue.dequeue(timeout=1)
        if chunk_id is None:
            break
        await worker.process_one(chunk_id, "host/pid-1/c0")

    # --- Result: read back through the API's client -------------------------
    final = await api_store.get_job(job.job_id)
    assert final.status is Status.DONE
    assert final.transcript == "0 1 2 3"  # reassembled in order across the queue

    lineage = await api_store.list_chunks(job.job_id)
    assert all(c.status is Status.DONE for c in lineage)
    assert all(c.worker_id == "host/pid-1/c0" for c in lineage)
    assert all(c.backend_used is Backend.LOCAL for c in lineage)

    await api_r.aclose()
    await worker_r.aclose()
