"""Hosted backend: transcribe via the Hugging Face Inference API (hf-inference).

We call the router endpoint directly over HTTP rather than through
`huggingface_hub`'s high-level client: in current versions the default
`provider="auto"` fails to resolve a provider for Whisper models (raises
StopIteration), and the client omits the request Content-Type for raw audio. A
direct POST with an explicit `Content-Type` is simpler and robust.

Inference is a network call, so this backend is I/O-bound — asyncio gives real
concurrency and a semaphore caps concurrent calls (a rate-limit guard against HF
429s). On HF's free serverless tier only larger Whisper models are served, so the
router (factory) points hosted requests at the strong model. httpx/soundfile are
imported lazily.
"""

from __future__ import annotations

import asyncio
import io

import numpy as np

from app.backends.http_base import HttpBackend
from app.logging import get_logger
from app.models import Backend

log = get_logger("backend.hosted")

_ROUTER_URL = "https://router.huggingface.co/hf-inference/models/{model}"
_MAX_RETRIES = 2          # HF may return 503 while a model warms up
_RETRY_WAIT_S = 3.0


class HostedBackend(HttpBackend):
    kind = Backend.HOSTED

    def __init__(self, token: str, max_concurrency: int) -> None:
        super().__init__(timeout=120.0)
        self._token = token
        self._sem = asyncio.Semaphore(max(1, max_concurrency))

    def available(self) -> bool:
        return bool(self._token)

    @staticmethod
    def _encode_flac(samples: np.ndarray, sample_rate: int) -> bytes:
        import soundfile as sf  # lazy import

        buf = io.BytesIO()
        sf.write(buf, samples, sample_rate, format="FLAC")
        return buf.getvalue()

    async def transcribe(
        self, samples: np.ndarray, sample_rate: int, model_id: str
    ) -> str:
        if not self.available():
            raise RuntimeError("hosted backend has no Hugging Face token configured")

        audio = self._encode_flac(samples, sample_rate)
        url = _ROUTER_URL.format(model=model_id)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "audio/flac",
        }
        client = self._get_client()

        last_error = ""
        for attempt in range(_MAX_RETRIES + 1):
            async with self._sem:  # admission control / rate-limit guard
                resp = await client.post(url, content=audio, headers=headers)
            if resp.status_code == 200:
                return self._extract_text(resp.json())
            last_error = f"{resp.status_code}: {resp.text[:200]}"
            # 503 = model still loading on HF's side; back off and retry.
            if resp.status_code == 503 and attempt < _MAX_RETRIES:
                log.info("hosted.warming", model=model_id, attempt=attempt)
                await asyncio.sleep(_RETRY_WAIT_S)
                continue
            break
        raise RuntimeError(f"HF inference failed ({last_error})")
