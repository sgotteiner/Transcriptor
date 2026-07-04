"""Backend router: resolve (tier, requested backend) into an execution plan.

Enforces the deliberate model↔backend matrix:
  - local            -> fast (tiny) only, on-box
  - remote + fast    -> tiny, compute offloaded
  - remote + strong  -> small (larger via env)
The hard rule: **strong => remote**. The fast tier may fall back from hosted to
local when hosted is unavailable and fallback is enabled; the strong tier cannot
(there is no local strong model), so it fails clearly.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.backends.base import TranscriptionBackend
from app.backends.hosted import HostedBackend
from app.backends.local import LocalBackend
from app.backends.model_server import ModelServerBackend
from app.config import Settings
from app.models import Backend, Tier


class BackendUnavailable(RuntimeError):
    """A requested (tier, backend) combination cannot be served right now."""


@dataclass(frozen=True)
class BackendPlan:
    backend: Backend        # the backend to use first
    model_id: str           # which model that backend should run
    allow_local_fallback: bool  # retry on local if the primary fails


class BackendRouter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # The "local" tier runs in-process by default, or delegates to a shared
        # model-server (one model in RAM for all workers) when a URL is configured.
        if settings.model_server_url:
            self._local: TranscriptionBackend = ModelServerBackend(settings.model_server_url)
        else:
            self._local = LocalBackend(pool_size=settings.max_concurrent_chunks)
        self._hosted = HostedBackend(
            token=settings.hf_token,
            max_concurrency=settings.hosted_max_concurrency,
        )

    def get(self, backend: Backend) -> TranscriptionBackend:
        return self._local if backend is Backend.LOCAL else self._hosted

    def plan(self, tier: Tier, requested: Backend) -> BackendPlan:
        s = self._settings

        if tier is Tier.STRONG:
            # Strong is remote-only; there is no local strong model to fall back to.
            if not self._hosted.available():
                raise BackendUnavailable(
                    "the strong model is remote-only and requires a Hugging Face "
                    "token (set TRANSCRIPTOR_HF_TOKEN)"
                )
            return BackendPlan(Backend.HOSTED, s.model_strong, allow_local_fallback=False)

        # Fast tier (tiny), runnable either locally or remotely.
        if requested is Backend.LOCAL:
            return BackendPlan(Backend.LOCAL, s.model_fast, allow_local_fallback=False)

        # Requested hosted-fast. HF's free serverless tier serves only the larger
        # Whisper model, so hosted requests always use the strong model.
        if self._hosted.available():
            return BackendPlan(
                Backend.HOSTED, s.model_strong, allow_local_fallback=s.fallback_to_local
            )
        if s.fallback_to_local:
            return BackendPlan(Backend.LOCAL, s.model_fast, allow_local_fallback=False)
        raise BackendUnavailable(
            "hosted backend requested but no Hugging Face token is configured"
        )

    async def aclose(self) -> None:
        await self._local.aclose()
        await self._hosted.aclose()
