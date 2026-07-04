"""Application settings, loaded from the environment (and an optional .env file).

Every setting has a default that lets the local `tiny` backend run with no
configuration at all. See .env.example for the full annotated list.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRANSCRIPTOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Auth ---------------------------------------------------------------
    # Empty token => auth disabled (dev convenience). Any non-empty value is
    # required as a Bearer token / X-API-Key header on protected endpoints.
    api_token: str = ""

    # --- Models -------------------------------------------------------------
    # Local backend loads `model_fast` only. `model_strong` is remote-only.
    model_fast: str = "openai/whisper-tiny"          # local, on-box
    model_strong: str = "openai/whisper-large-v3"    # hosted (HF serves this tier)

    # --- Hugging Face hosted backend ---------------------------------------
    hf_token: str = ""

    # --- Redis (Stage B) ----------------------------------------------------
    # Empty => in-memory state + in-process async (Stage A). When set, async jobs
    # are queued in Redis and drained by separate worker processes; state/lineage
    # live in Redis (shared across API and workers). e.g. redis://localhost:6379/0
    redis_url: str = ""

    # --- Shared inference server (optional) ---------------------------------
    # Empty => local inference runs in-process (model loaded per process). When set,
    # the "local" backend delegates to a shared model-server over HTTP, so the model
    # is loaded once instead of once per worker. e.g. http://model-server:8001
    model_server_url: str = ""

    # --- Chunking -----------------------------------------------------------
    chunk_seconds: float = 20.0
    chunk_overlap_seconds: float = 2.0

    # --- Concurrency / admission control ------------------------------------
    max_concurrent_chunks: int = 4
    hosted_max_concurrency: int = 4

    # --- Behaviour ----------------------------------------------------------
    fallback_to_local: bool = True
    max_upload_mb: int = 200

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_token)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so the whole process shares one Settings instance."""
    return Settings()
