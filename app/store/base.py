"""State store interface.

Holds jobs and their chunks (with lineage). Stage A ships an in-memory
implementation; Stage B swaps in a Redis-backed one behind this same interface so
state — and "which worker touched this chunk" — becomes shared across pods. The
methods are async so the Redis implementation is a drop-in.
"""

from __future__ import annotations

import abc

from app.models import Chunk, Job


class StateStore(abc.ABC):
    @abc.abstractmethod
    async def create_job(self, job: Job) -> None: ...

    @abc.abstractmethod
    async def get_job(self, job_id: str) -> Job | None: ...

    @abc.abstractmethod
    async def save_job(self, job: Job) -> None: ...

    @abc.abstractmethod
    async def add_chunks(self, chunks: list[Chunk]) -> None: ...

    @abc.abstractmethod
    async def save_chunk(self, chunk: Chunk) -> None: ...

    @abc.abstractmethod
    async def get_chunk(self, chunk_id: str) -> Chunk | None: ...

    @abc.abstractmethod
    async def list_chunks(self, job_id: str) -> list[Chunk]: ...
