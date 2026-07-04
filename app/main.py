"""FastAPI application: wiring, lifespan, static UI.

On startup we build the process-wide singletons (settings, state store, backend
router, priority scheduler, pipeline) and start the scheduler's worker tasks; on
shutdown we stop them cleanly. Stage A uses the in-memory store; Stage B swaps in
Redis behind the same interfaces without touching this file's shape.
"""

from __future__ import annotations

import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import routes_health, routes_jobs
from app.backends.factory import BackendRouter
from app.config import get_settings
from app.core.pipeline import Pipeline
from app.core.scheduler import PriorityScheduler
from app.logging import configure_logging, get_logger
from app.store.memory import MemoryStateStore

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging()
    log = get_logger()

    router = BackendRouter(settings)
    scheduler = PriorityScheduler(settings.max_concurrent_chunks)
    await scheduler.start()

    # Redis-backed (shared) state + async queue when configured; else in-process.
    redis_client = None
    async_queue = None
    if settings.redis_url:
        import redis.asyncio as aioredis

        from app.store.redis_store import RedisQueue, RedisStateStore

        redis_client = aioredis.Redis.from_url(settings.redis_url)
        store = RedisStateStore(redis_client)
        async_queue = RedisQueue(redis_client)
    else:
        store = MemoryStateStore()

    app.state.settings = settings
    app.state.store = store
    app.state.router = router
    app.state.scheduler = scheduler
    app.state.pipeline = Pipeline(settings, store, router, scheduler, async_queue)

    log.info("app.started", auth=settings.auth_enabled,
             concurrency=settings.max_concurrent_chunks,
             backend_store="redis" if settings.redis_url else "memory")
    try:
        yield
    finally:
        await scheduler.stop()
        await router.aclose()
        if redis_client is not None:
            await redis_client.aclose()
        log.info("app.stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Transcriptor", version="0.1.0", lifespan=lifespan)
    app.include_router(routes_health.router)
    app.include_router(routes_jobs.router)

    # Mounted last so API routes (/jobs, /healthz, /docs) take precedence; the
    # static mount then serves the UI at "/" and its assets by relative path.
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app


app = create_app()
