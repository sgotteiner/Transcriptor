"""Shared plumbing for HTTP backends (HF router + self-hosted model-server).

Both POST audio to a remote endpoint and read text back; this base owns the lazy
httpx client, cleanup, and response parsing, so each backend only implements its own
encoding / endpoint / retry policy. httpx is imported lazily.
"""

from __future__ import annotations

from app.backends.base import TranscriptionBackend


class HttpBackend(TranscriptionBackend):
    def __init__(self, timeout: float = 120.0) -> None:
        self._timeout = timeout
        self._client = None  # httpx.AsyncClient, built lazily

    def _get_client(self):
        if self._client is None:
            import httpx  # lazy import

            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._client

    @staticmethod
    def _extract_text(payload) -> str:
        """Whisper responses come back as {"text": ...}, a list of those, or a str."""
        if isinstance(payload, dict):
            return (payload.get("text") or "").strip()
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return (payload[0].get("text") or "").strip()
        return str(payload).strip()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
