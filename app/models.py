"""Domain model: the Job (molecule) and the Chunk (the atom / unit of work + lineage).

In the design docs a chunk is nicknamed an "atom"; in the code it is always a
`Chunk`. Every Chunk carries its own lineage: what happened to it, where, and when.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, computed_field


class Status(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class Mode(str, Enum):
    SYNC = "sync"      # real-time, streamed in order
    ASYNC = "async"    # queued, polled


class Tier(str, Enum):
    FAST = "fast"      # whisper-tiny
    STRONG = "strong"  # whisper-small (remote only)


class Backend(str, Enum):
    LOCAL = "local"
    HOSTED = "hosted"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Chunk(BaseModel):
    """One time-slice of the media — the unit of work and the unit of lineage."""

    # Ignore extras on load so JSON round-trips through Redis (the computed
    # `duration_ms` is serialized out but recomputed, not set, on the way back in).
    model_config = ConfigDict(extra="ignore")

    chunk_id: str = Field(default_factory=lambda: _new_id("chunk"))
    job_id: str
    index: int

    # Content span within the source media.
    start_ms: int
    end_ms: int
    overlap_ms: int = 0

    # How / where it was processed (lineage).
    tier: Tier
    backend_requested: Backend
    backend_used: Backend | None = None
    worker_id: str | None = None
    priority: int = 0  # lower = more urgent; sync < async

    # Outcome.
    status: Status = Status.PENDING
    retries: int = 0
    text: str = ""
    error: str | None = None

    # Timings.
    enqueued_at: int = Field(default_factory=_now_ms)
    started_at: int | None = None
    finished_at: int | None = None

    @computed_field  # included in model_dump / API responses
    @property
    def duration_ms(self) -> int | None:
        if self.started_at is not None and self.finished_at is not None:
            return self.finished_at - self.started_at
        return None

    def mark_started(self, worker_id: str, backend_used: Backend) -> None:
        self.status = Status.PROCESSING
        self.worker_id = worker_id
        self.backend_used = backend_used
        self.started_at = _now_ms()

    def mark_done(self, text: str) -> None:
        self.status = Status.DONE
        self.text = text
        self.finished_at = _now_ms()

    def mark_failed(self, error: str) -> None:
        self.status = Status.FAILED
        self.error = error
        self.finished_at = _now_ms()


class Job(BaseModel):
    """A single upload: a set of chunks plus the reassembled transcript."""

    model_config = ConfigDict(extra="ignore")

    job_id: str = Field(default_factory=lambda: _new_id("job"))
    created_at: int = Field(default_factory=_now_ms)
    status: Status = Status.PENDING
    mode: Mode
    tier: Tier
    backend_requested: Backend
    source_filename: str
    duration_ms: int | None = None
    chunk_count: int = 0
    transcript: str = ""
    error: str | None = None

    def public_dict(self, chunks: list[Chunk] | None = None) -> dict:
        """Serialisable view for API responses."""
        data = self.model_dump(mode="json")
        if chunks is not None:
            data["chunks"] = [c.model_dump(mode="json") for c in chunks]
        return data
