"""Local backend: run Whisper in-process on the CPU.

The model is loaded **once per process** and shared across all threads — never one
model per thread. Blocking inference is pushed onto a bounded thread pool so it
stays off the asyncio event loop; the pool size is the local admission cap. On a
single box this buys responsiveness and delivery latency, not raw throughput
(Torch already spreads one inference across cores). transformers/torch are imported
lazily so the rest of the app (and unit tests) import without heavy deps present.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

from app.backends.base import TranscriptionBackend
from app.core.chunking import SAMPLE_RATE
from app.logging import get_logger
from app.models import Backend

log = get_logger("backend.local")


class LocalBackend(TranscriptionBackend):
    kind = Backend.LOCAL

    def __init__(self, pool_size: int) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, pool_size), thread_name_prefix="whisper"
        )
        self._pipelines: dict[str, Any] = {}
        self._lock = threading.Lock()

    def available(self) -> bool:
        return True

    def _get_pipeline(self, model_id: str) -> Any:
        """Lazily build and cache one ASR pipeline per model id."""
        pipe = self._pipelines.get(model_id)
        if pipe is not None:
            return pipe
        with self._lock:
            pipe = self._pipelines.get(model_id)
            if pipe is None:
                from transformers import pipeline  # lazy, heavy import

                log.info("model.loading", model_id=model_id)
                pipe = pipeline(
                    task="automatic-speech-recognition",
                    model=model_id,
                    chunk_length_s=30,  # transformers' internal windowing
                )
                self._pipelines[model_id] = pipe
                log.info("model.loaded", model_id=model_id)
        return pipe

    def _run(self, samples: np.ndarray, model_id: str) -> str:
        pipe = self._get_pipeline(model_id)
        result = pipe({"raw": samples, "sampling_rate": SAMPLE_RATE})
        return (result.get("text") or "").strip()

    async def transcribe(
        self, samples: np.ndarray, sample_rate: int, model_id: str
    ) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, self._run, samples, model_id)

    async def aclose(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
