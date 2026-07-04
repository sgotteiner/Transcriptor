"""Orchestration: decode → chunk → schedule → transcribe → reassemble, with lineage.

One code path serves both modes. `stream_sync` yields ordered deltas for the SSE
endpoint; `run_async` drives the job in the background for polling. Chunk execution
(`_execute`) is shared and never raises — a failed chunk is recorded in its lineage
and contributes empty text, so the in-order prefix never stalls on one bad chunk.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import numpy as np

from app.backends.factory import BackendRouter
from app.core.chunking import SAMPLE_RATE, chunk_file
from app.core.reassembly import InOrderAssembler
from app.core.scheduler import PriorityScheduler
from app.config import Settings
from app.logging import get_logger
from app.models import Backend, Chunk, Job, Mode, Status

log = get_logger("pipeline")

# (chunk, its audio samples)
ChunkWork = tuple[Chunk, np.ndarray]


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store,
        router: BackendRouter,
        scheduler: PriorityScheduler | None = None,
        async_queue=None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._router = router
        self._scheduler = scheduler        # None in worker processes
        self._async_queue = async_queue    # set => async goes through Redis
        self._bg: set[asyncio.Task] = set()

    # --- preparation --------------------------------------------------------

    async def prepare(self, job: Job, upload_path: str):
        """Resolve the backend, decode + chunk, persist chunks. Returns (work, plan).

        Raises BackendUnavailable if the requested (tier, backend) can't be served.
        """
        plan = self._router.plan(job.tier, job.backend_requested)  # may raise
        segments, duration_ms = chunk_file(
            upload_path,
            self._settings.chunk_seconds,
            self._settings.chunk_overlap_seconds,
        )

        mode_class = 0 if job.mode is Mode.SYNC else 1
        work: list[ChunkWork] = []
        chunks: list[Chunk] = []
        for seg in segments:
            chunk = Chunk(
                job_id=job.job_id,
                index=seg.index,
                start_ms=seg.start_ms,
                end_ms=seg.end_ms,
                overlap_ms=seg.overlap_ms,
                tier=job.tier,
                backend_requested=job.backend_requested,
                priority=mode_class,
            )
            chunks.append(chunk)
            work.append((chunk, seg.samples))

        job.duration_ms = duration_ms
        job.chunk_count = len(chunks)
        job.status = Status.PROCESSING
        await self._store.add_chunks(chunks)
        await self._store.save_job(job)
        log.info(
            "job.prepared",
            job_id=job.job_id,
            chunks=len(chunks),
            duration_ms=duration_ms,
            backend=plan.backend.value,
            model=plan.model_id,
        )
        return work, plan

    # --- sync (streamed, in order) -----------------------------------------

    async def stream_sync(self, job: Job, work: list[ChunkWork], plan) -> AsyncIterator[dict]:
        total = len(work)
        assembler = InOrderAssembler(total)
        # Emit the job_id up front so the client can start polling lineage
        # immediately, rather than only after the first chunk finishes.
        yield {"type": "accepted", "job_id": job.job_id, "completed": 0, "total": total}
        if total == 0:
            await self._finalize(job)
            yield {"type": "done", "job_id": job.job_id,
                   "transcript": job.transcript, "status": job.status.value}
            return

        index_by_fut = {}
        for chunk, samples in work:
            fut = self._scheduler.submit(
                (chunk.priority, job.created_at, chunk.index),
                self._factory(chunk, samples, plan),
            )
            index_by_fut[fut] = chunk.index

        tasks = [asyncio.create_task(self._indexed(i, f)) for f, i in index_by_fut.items()]
        completed = 0
        for coro in asyncio.as_completed(tasks):
            index, text = await coro
            completed += 1
            delta = assembler.add(index, text)
            yield {
                "type": "progress",
                "job_id": job.job_id,
                "index": index,
                "completed": completed,
                "total": total,
                "delta": delta,
                "transcript": assembler.transcript,
            }

        await self._finalize(job)
        yield {"type": "done", "job_id": job.job_id,
               "transcript": job.transcript, "status": job.status.value}

    # --- async (queued, polled) --------------------------------------------

    async def run_async(self, job: Job, work: list[ChunkWork], plan) -> None:
        """Kick off async processing and return immediately.

        With a Redis queue configured, enqueue chunks for separate worker processes
        to drain; otherwise run in-process on the local scheduler (Stage A).
        """
        if self._async_queue is not None:
            if not work:
                await self._finalize(job, "")
                return
            await self._async_queue.set_remaining(job.job_id, len(work))
            for chunk, samples in work:
                await self._async_queue.enqueue(chunk.chunk_id, samples)
            return

        task = asyncio.create_task(self._drive_async(job, work, plan))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _drive_async(self, job: Job, work: list[ChunkWork], plan) -> None:
        if work:
            futures = [
                self._scheduler.submit(
                    (chunk.priority, job.created_at, chunk.index),
                    self._factory(chunk, samples, plan),
                )
                for chunk, samples in work
            ]
            await asyncio.gather(*futures)  # wait for every chunk to finish
        await self._finalize(job)

    # --- shared chunk execution --------------------------------------------

    def _factory(self, chunk: Chunk, samples: np.ndarray, plan):
        async def run(worker_id: str) -> str:
            return await self._execute(chunk, samples, plan, worker_id)

        return run

    async def _execute(self, chunk: Chunk, samples: np.ndarray, plan, worker_id: str) -> str:
        clog = log.bind(job_id=chunk.job_id, chunk_id=chunk.chunk_id, worker_id=worker_id)
        chunk.mark_started(worker_id, plan.backend)
        await self._store.save_chunk(chunk)
        try:
            text = await self._router.get(plan.backend).transcribe(
                samples, SAMPLE_RATE, plan.model_id
            )
            chunk.mark_done(text)
            await self._store.save_chunk(chunk)
            clog.info("chunk.done", backend=plan.backend.value, ms=chunk.duration_ms)
            return text
        except Exception as exc:
            clog.warning("chunk.primary_failed", backend=plan.backend.value, error=str(exc))
            if plan.allow_local_fallback:
                text = await self._fallback_local(chunk, samples, clog)
                if text is not None:
                    return text
            chunk.mark_failed(str(exc))
            await self._store.save_chunk(chunk)
            clog.error("chunk.failed", error=str(exc))
            return ""

    async def _fallback_local(self, chunk: Chunk, samples: np.ndarray, clog) -> str | None:
        """Retry a failed chunk on the local backend. Returns text, or None if unable."""
        local = self._router.get(Backend.LOCAL)
        if not local.available():
            return None
        try:
            chunk.retries += 1
            chunk.backend_used = Backend.LOCAL
            text = await local.transcribe(samples, SAMPLE_RATE, self._settings.model_fast)
            chunk.mark_done(text)
            await self._store.save_chunk(chunk)
            clog.info("chunk.fallback_done")
            return text
        except Exception as exc:  # pragma: no cover - defensive
            clog.warning("chunk.fallback_failed", error=str(exc))
            return None

    async def _finalize(self, job: Job) -> None:
        """Assemble the final transcript from stored chunks and mark the job done.

        The single source of truth for the assembled transcript — used by the sync,
        in-process-async, and Redis-worker paths so they can't diverge.
        """
        chunks = await self._store.list_chunks(job.job_id)  # sorted by index
        assembler = InOrderAssembler(len(chunks))
        for c in chunks:
            assembler.add(c.index, c.text)
        failed = [c for c in chunks if c.status is Status.FAILED]
        job.transcript = assembler.transcript
        job.status = Status.FAILED if failed else Status.DONE
        if failed:
            job.error = f"{len(failed)} of {len(chunks)} chunks failed"
        await self._store.save_job(job)
        log.info("job.finalized", job_id=job.job_id, status=job.status.value,
                 failed=len(failed))

    @staticmethod
    async def _indexed(index: int, fut: asyncio.Future) -> tuple[int, str]:
        text = await fut
        return index, text
