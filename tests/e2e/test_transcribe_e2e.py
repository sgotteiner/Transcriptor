"""End-to-end test: transcribe a real recording through the real local pipeline.

Drop your own clip at tests/e2e/fixtures/sample.<ext> and the expected words at
tests/e2e/fixtures/expected.txt. The test asserts a word-error-rate below a
threshold (ASR is never character-exact, so a fuzzy criterion is the right one).
It downloads the tiny model on first run, so it is marked `e2e` and skipped unless
the fixtures exist. Run with:  pytest -m e2e
"""

import glob
import os

import pytest

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
WER_THRESHOLD = 0.4  # generous: tiny model + short clip


def _find_sample() -> str | None:
    for path in glob.glob(os.path.join(FIXTURES, "sample.*")):
        return path
    return None


def _expected() -> str | None:
    path = os.path.join(FIXTURES, "expected.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return None


@pytest.mark.e2e
async def test_real_recording_transcribes_within_wer():
    sample, expected = _find_sample(), _expected()
    if not sample or not expected:
        pytest.skip("no fixture recording; add sample.<ext> + expected.txt")

    import jiwer

    from app.config import Settings
    from app.core.pipeline import Pipeline
    from app.core.scheduler import PriorityScheduler
    from app.backends.factory import BackendRouter
    from app.models import Backend, Job, Mode, Tier
    from app.store.memory import MemoryStateStore

    settings = Settings()
    store = MemoryStateStore()
    scheduler = PriorityScheduler(settings.max_concurrent_chunks)
    await scheduler.start()
    router = BackendRouter(settings)
    pipe = Pipeline(settings, store, router, scheduler)
    try:
        job = Job(mode=Mode.SYNC, tier=Tier.FAST, backend_requested=Backend.LOCAL,
                  source_filename=os.path.basename(sample))
        await store.create_job(job)
        work, plan = await pipe.prepare(job, sample)
        events = [e async for e in pipe.stream_sync(job, work, plan)]
        transcript = events[-1]["transcript"]

        wer = jiwer.wer(
            jiwer.ToLowerCase()(expected),
            jiwer.ToLowerCase()(transcript),
        )
        assert wer <= WER_THRESHOLD, f"WER {wer:.2f} > {WER_THRESHOLD}: {transcript!r}"
    finally:
        await scheduler.stop()
        await router.aclose()
