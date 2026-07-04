"""The backend abstraction.

The whole pipeline — chunking, scheduling, reassembly, lineage — is identical no
matter where inference runs. A backend is the one pluggable leaf: given a chunk's
audio and a model id, return text. Each backend also decides its own concurrency
primitive (thread pool for local CPU, coroutines for hosted network I/O).
"""

from __future__ import annotations

import abc

import numpy as np

from app.models import Backend


class TranscriptionBackend(abc.ABC):
    kind: Backend

    @abc.abstractmethod
    async def transcribe(
        self, samples: np.ndarray, sample_rate: int, model_id: str
    ) -> str:
        """Transcribe one chunk's audio. Raises on failure."""

    def available(self) -> bool:
        """Whether this backend can currently serve requests."""
        return True

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        """Release any resources (thread pools, HTTP clients)."""
