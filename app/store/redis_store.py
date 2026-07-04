"""Redis-backed state store and async work queue (Stage B).

State (jobs + chunk lineage) lives in Redis so it is shared across the API and any
number of separate worker processes — which is what makes "which worker processed
this chunk" a real cross-process fact and lets async work fan out. Same
`StateStore` interface as the in-memory version, so the rest of the app is
unchanged.

The module is named `redis_store` (not `redis`) so it doesn't shadow the `redis`
package. The client is created with `decode_responses=False` because chunk audio is
stored as raw bytes; JSON values are encoded/decoded explicitly.

Shortcut (documented): chunk audio is parked in Redis as raw float32 bytes. That is
fine for a take-home; a production system would use object storage (S3/MinIO) and
keep only a reference in Redis.
"""

from __future__ import annotations

import numpy as np

from app.models import Chunk, Job
from app.store.base import StateStore

QUEUE_KEY = "queue:chunks"


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _job_chunks_key(job_id: str) -> str:
    return f"job:{job_id}:chunks"


def _remaining_key(job_id: str) -> str:
    return f"job:{job_id}:remaining"


def _chunk_key(chunk_id: str) -> str:
    return f"chunk:{chunk_id}"


def _audio_key(chunk_id: str) -> str:
    return f"chunk:{chunk_id}:audio"


class RedisStateStore(StateStore):
    def __init__(self, client) -> None:
        self._r = client  # redis.asyncio.Redis

    async def create_job(self, job: Job) -> None:
        await self._r.set(_job_key(job.job_id), job.model_dump_json())

    async def get_job(self, job_id: str) -> Job | None:
        raw = await self._r.get(_job_key(job_id))
        return Job.model_validate_json(raw) if raw else None

    async def save_job(self, job: Job) -> None:
        await self._r.set(_job_key(job.job_id), job.model_dump_json())

    async def add_chunks(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        pipe = self._r.pipeline()
        for chunk in chunks:
            pipe.set(_chunk_key(chunk.chunk_id), chunk.model_dump_json())
            pipe.rpush(_job_chunks_key(chunk.job_id), chunk.chunk_id)
        await pipe.execute()

    async def save_chunk(self, chunk: Chunk) -> None:
        await self._r.set(_chunk_key(chunk.chunk_id), chunk.model_dump_json())

    async def get_chunk(self, chunk_id: str) -> Chunk | None:
        raw = await self._r.get(_chunk_key(chunk_id))
        return Chunk.model_validate_json(raw) if raw else None

    async def list_chunks(self, job_id: str) -> list[Chunk]:
        ids = await self._r.lrange(_job_chunks_key(job_id), 0, -1)
        chunks = []
        for cid in ids:
            chunk = await self.get_chunk(cid.decode())
            if chunk is not None:
                chunks.append(chunk)
        return sorted(chunks, key=lambda c: c.index)


class RedisQueue:
    """Shared work queue: chunk ids on a Redis list, audio + counters alongside."""

    def __init__(self, client) -> None:
        self._r = client

    async def enqueue(self, chunk_id: str, samples: np.ndarray) -> None:
        await self._r.set(_audio_key(chunk_id), samples.astype(np.float32).tobytes())
        await self._r.rpush(QUEUE_KEY, chunk_id)

    async def dequeue(self, timeout: int = 5) -> str | None:
        res = await self._r.blpop([QUEUE_KEY], timeout=timeout)  # FIFO (rpush/blpop)
        return res[1].decode() if res else None

    async def load_audio(self, chunk_id: str) -> np.ndarray:
        raw = await self._r.get(_audio_key(chunk_id))
        return np.frombuffer(raw, dtype=np.float32) if raw else np.array([], np.float32)

    async def discard_audio(self, chunk_id: str) -> None:
        await self._r.delete(_audio_key(chunk_id))

    async def set_remaining(self, job_id: str, n: int) -> None:
        await self._r.set(_remaining_key(job_id), n)

    async def decr_remaining(self, job_id: str) -> int:
        return int(await self._r.decr(_remaining_key(job_id)))
