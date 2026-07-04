"""Shared inference server (optional container).

Loads Whisper **once** and serves transcription over HTTP, so the API and every
worker can be thin clients (`ModelServerBackend`) instead of each loading its own
model copy into RAM. Run as its own container/process:

    uvicorn app.model_server:app --host 0.0.0.0 --port 8001

Internally it just reuses `LocalBackend` (the same in-process Whisper path), so
behaviour and lineage are identical — the model simply lives in one place. See D21.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, Query, Request

from app.backends.local import LocalBackend
from app.config import get_settings
from app.logging import configure_logging, get_logger

log = get_logger("model_server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging()
    backend = LocalBackend(pool_size=settings.max_concurrent_chunks)
    app.state.backend = backend
    # Warm the default model at startup so /healthz only passes once it's ready
    # (blocking load off the event loop).
    log.info("model_server.warming", model=settings.model_fast)
    await asyncio.get_running_loop().run_in_executor(
        None, backend._get_pipeline, settings.model_fast
    )
    log.info("model_server.ready", model=settings.model_fast)
    try:
        yield
    finally:
        await backend.aclose()


app = FastAPI(title="Transcriptor Model Server", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/transcribe")
async def transcribe(
    request: Request,
    model_id: str = Query(...),
    sample_rate: int = Query(16000),
) -> dict:
    raw = await request.body()
    samples = np.frombuffer(raw, dtype=np.float32)
    text = await request.app.state.backend.transcribe(samples, sample_rate, model_id)
    return {"text": text}
