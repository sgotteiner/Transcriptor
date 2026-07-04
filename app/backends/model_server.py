"""Model-server backend: delegate local inference to a shared inference container.

When `TRANSCRIPTOR_MODEL_SERVER_URL` is set, the factory uses this in place of the
in-process `LocalBackend`, so the API and every worker become thin HTTP clients and
the Whisper model is loaded **once** (in the model-server), not once per process.
This is the self-hosted counterpart of the `hosted` backend (D18/D21): same
`TranscriptionBackend` interface, an internal URL instead of the HF router. Reports
`backend_used=local` in lineage — it *is* the local tier, just served out-of-process.
"""

from __future__ import annotations

import numpy as np

from app.backends.http_base import HttpBackend
from app.models import Backend


class ModelServerBackend(HttpBackend):
    kind = Backend.LOCAL

    def __init__(self, url: str, timeout: float = 120.0) -> None:
        super().__init__(timeout=timeout)
        self._url = url.rstrip("/") + "/transcribe"

    def available(self) -> bool:
        return bool(self._url)

    async def transcribe(
        self, samples: np.ndarray, sample_rate: int, model_id: str
    ) -> str:
        resp = await self._get_client().post(
            self._url,
            params={"model_id": model_id, "sample_rate": sample_rate},
            content=samples.astype(np.float32).tobytes(),
            headers={"Content-Type": "application/octet-stream"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"model server {resp.status_code}: {resp.text[:200]}")
        return self._extract_text(resp.json())
