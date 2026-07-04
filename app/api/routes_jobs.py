"""Job endpoints: create (sync SSE or async), read status, read lineage, tail stream."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from sse_starlette.sse import EventSourceResponse

from app.auth import require_token
from app.backends.factory import BackendUnavailable
from app.core.chunking import DecodeError
from app.core.reassembly import InOrderAssembler
from app.models import Backend, Job, Mode, Status, Tier

router = APIRouter(tags=["jobs"], dependencies=[Depends(require_token)])


def _pipeline(request: Request):
    return request.app.state.pipeline


def _store(request: Request):
    return request.app.state.store


def _parse_enum(enum_cls, value: str, field: str):
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(e.value for e in enum_cls)
        raise HTTPException(400, f"invalid {field} '{value}'; allowed: {allowed}")


async def _save_upload(request: Request, upload: UploadFile) -> str:
    """Stream the upload to a temp file, enforcing the size cap. Returns the path."""
    max_bytes = request.app.state.settings.max_upload_bytes
    fd, path = tempfile.mkstemp(suffix=f"_{upload.filename or 'upload'}")
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, "uploaded file exceeds the size limit")
                f.write(chunk)
    except Exception:
        os.unlink(path)
        raise
    if size == 0:
        os.unlink(path)
        raise HTTPException(400, "uploaded file is empty")
    return path


@router.post("/jobs")
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    tier: str = Form("fast"),
    mode: str = Form("sync"),
    backend: str = Form("local"),
):
    job = Job(
        mode=_parse_enum(Mode, mode, "mode"),
        tier=_parse_enum(Tier, tier, "tier"),
        backend_requested=_parse_enum(Backend, backend, "backend"),
        source_filename=file.filename or "upload",
    )

    path = await _save_upload(request, file)
    pipeline = _pipeline(request)
    await _store(request).create_job(job)

    try:
        work, plan = await pipeline.prepare(job, path)
    except BackendUnavailable as exc:
        raise HTTPException(400, str(exc))
    except DecodeError as exc:
        raise HTTPException(400, f"could not decode media: {exc}")
    finally:
        # Audio is fully decoded into memory by prepare(); the file is no longer needed.
        if os.path.exists(path):
            os.unlink(path)

    if job.mode is Mode.SYNC:
        async def events():
            async for evt in pipeline.stream_sync(job, work, plan):
                yield {"data": json.dumps(evt)}

        return EventSourceResponse(events())

    await pipeline.run_async(job, work, plan)
    return {"job_id": job.job_id, "status": job.status.value, "chunk_count": job.chunk_count}


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    job = await _store(request).get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.public_dict()


@router.get("/jobs/{job_id}/chunks")
async def get_chunks(request: Request, job_id: str):
    store = _store(request)
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    chunks = await store.list_chunks(job_id)
    return {"job_id": job_id, "chunks": [c.model_dump(mode="json") for c in chunks]}


@router.get("/jobs/{job_id}/stream")
async def stream_job(request: Request, job_id: str):
    """Tail a job's progress as SSE (works for async jobs) by polling lineage."""
    store = _store(request)
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")

    async def events():
        last = None
        while True:
            j = await store.get_job(job_id)
            chunks = await store.list_chunks(job_id)
            assembler = InOrderAssembler(j.chunk_count)
            for c in chunks:
                if c.status in (Status.DONE, Status.FAILED):
                    assembler.add(c.index, c.text)
            transcript = assembler.transcript
            done = sum(1 for c in chunks if c.status in (Status.DONE, Status.FAILED))
            snapshot = (transcript, done, j.status.value)
            if snapshot != last:
                last = snapshot
                yield {"data": json.dumps({
                    "type": "progress", "job_id": job_id,
                    "completed": done, "total": j.chunk_count,
                    "transcript": transcript, "status": j.status.value,
                })}
            if j.status in (Status.DONE, Status.FAILED):
                yield {"data": json.dumps({
                    "type": "done", "job_id": job_id,
                    "transcript": transcript, "status": j.status.value,
                })}
                return
            await asyncio.sleep(0.3)

    return EventSourceResponse(events())
