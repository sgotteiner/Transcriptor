"""Chunk splitting: window/overlap geometry, no ffmpeg needed (synthetic waveform)."""

import numpy as np
import pytest

from app.core.chunking import SAMPLE_RATE, split_samples


def test_splits_into_overlapping_windows():
    samples = np.zeros(SAMPLE_RATE * 50, dtype=np.float32)  # 50 seconds
    segs = split_samples(samples, window_seconds=20, overlap_seconds=2)

    assert [s.index for s in segs] == [0, 1, 2]  # ceil(50/20)

    # First chunk has no left overlap; later chunks carry 2s of context.
    assert segs[0].overlap_ms == 0
    assert segs[0].start_ms == 0 and segs[0].end_ms == 20_000
    assert segs[1].overlap_ms == 2_000
    assert segs[1].start_ms == 18_000 and segs[1].end_ms == 40_000
    assert segs[2].end_ms == 50_000


def test_empty_audio_yields_no_chunks():
    assert split_samples(np.array([], dtype=np.float32), 20, 2) == []


def test_overlap_must_be_less_than_window():
    with pytest.raises(ValueError):
        split_samples(np.zeros(10, dtype=np.float32), window_seconds=2, overlap_seconds=2)
