"""In-memory state store (Stage A).

Good enough to run and demo the whole pipeline with zero infrastructure. State is
process-local and lost on restart — a deliberate take-home shortcut; Stage B's
Redis store replaces it behind the same interface. A lock keeps concurrent job
updates consistent.
"""

from __future__ import annotations

import asyncio

from app.models import Chunk, Job
from app.store.base import StateStore


class MemoryStateStore(StateStore):
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._chunks: dict[str, dict[str, Chunk]] = {}  # job_id -> chunk_id -> Chunk
        self._by_id: dict[str, Chunk] = {}              # chunk_id -> Chunk
        self._lock = asyncio.Lock()

    async def create_job(self, job: Job) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job
            self._chunks.setdefault(job.job_id, {})

    async def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def save_job(self, job: Job) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job

    async def add_chunks(self, chunks: list[Chunk]) -> None:
        async with self._lock:
            for chunk in chunks:
                self._chunks.setdefault(chunk.job_id, {})[chunk.chunk_id] = chunk
                self._by_id[chunk.chunk_id] = chunk

    async def save_chunk(self, chunk: Chunk) -> None:
        async with self._lock:
            self._chunks.setdefault(chunk.job_id, {})[chunk.chunk_id] = chunk
            self._by_id[chunk.chunk_id] = chunk

    async def get_chunk(self, chunk_id: str) -> Chunk | None:
        return self._by_id.get(chunk_id)

    async def list_chunks(self, job_id: str) -> list[Chunk]:
        chunks = self._chunks.get(job_id, {})
        return sorted(chunks.values(), key=lambda c: c.index)
