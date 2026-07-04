"""Turn an arbitrary media file into ordered audio chunks (the map step).

We decode once to mono 16 kHz float32 PCM via a bundled ffmpeg binary (no system
install), then slice into fixed windows. Each chunk carries a small left-overlap
with its predecessor so words split across a boundary can be recovered by the
stitcher (see reassembly.py). Decoding to a numpy array up front means one ffmpeg
call and no separate ffprobe.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import imageio_ffmpeg
import numpy as np

SAMPLE_RATE = 16_000  # what Whisper expects


@dataclass(frozen=True)
class AudioSegment:
    """One audio chunk plus the metadata that becomes its lineage."""

    index: int
    start_ms: int      # where this chunk's audio begins (incl. left overlap)
    end_ms: int        # where it ends
    overlap_ms: int    # how much of the head duplicates the previous chunk
    samples: np.ndarray  # float32 mono @ 16 kHz


class DecodeError(RuntimeError):
    """ffmpeg could not decode the uploaded file."""


def decode_to_mono_16k(path: str) -> np.ndarray:
    """Decode any ffmpeg-readable media to a float32 mono waveform at 16 kHz."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg, "-nostdin", "-threads", "1",
        "-i", path,
        "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-hide_banner", "-loglevel", "error",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise DecodeError(detail or "ffmpeg failed to decode the media")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def split_samples(
    samples: np.ndarray,
    window_seconds: float,
    overlap_seconds: float,
) -> list[AudioSegment]:
    """Slice a waveform into ordered, left-overlapping windows.

    Chunk k covers the core span ``[k*window, (k+1)*window)`` plus ``overlap``
    seconds of the previous chunk prepended as context (except chunk 0). The
    stitcher removes the duplicated words at each seam.
    """
    if window_seconds <= 0:
        raise ValueError("window_seconds must be > 0")
    if overlap_seconds < 0 or overlap_seconds >= window_seconds:
        raise ValueError("overlap_seconds must be in [0, window_seconds)")

    total = len(samples)
    if total == 0:
        return []

    step = int(window_seconds * SAMPLE_RATE)
    overlap = int(overlap_seconds * SAMPLE_RATE)
    n_chunks = (total + step - 1) // step  # ceil

    segments: list[AudioSegment] = []
    for k in range(n_chunks):
        core_start = k * step
        audio_start = max(0, core_start - overlap)
        end = min(core_start + step, total)
        segments.append(
            AudioSegment(
                index=k,
                start_ms=_samples_to_ms(audio_start),
                end_ms=_samples_to_ms(end),
                overlap_ms=_samples_to_ms(core_start - audio_start),
                samples=samples[audio_start:end],
            )
        )
    return segments


def chunk_file(
    path: str,
    window_seconds: float,
    overlap_seconds: float,
) -> tuple[list[AudioSegment], int]:
    """Decode and split a file. Returns (segments, total_duration_ms)."""
    samples = decode_to_mono_16k(path)
    duration_ms = _samples_to_ms(len(samples))
    segments = split_samples(samples, window_seconds, overlap_seconds)
    return segments, duration_ms


def _samples_to_ms(n_samples: int) -> int:
    return int(round(n_samples * 1000 / SAMPLE_RATE))
