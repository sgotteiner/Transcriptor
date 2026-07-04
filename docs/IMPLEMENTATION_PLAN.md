# Transcriptor — Implementation (as built)

The concrete realisation of [DESIGN.md](./DESIGN.md): stack, module layout, exact
schemas/endpoints, config, and current status. Rationale for choices is in
[DECISIONS.md](./DECISIONS.md).

**Naming:** a chunk is nicknamed *atom* in design prose; in code and logs it is
always `chunk` / `chunk_id`.

---

## 1. Two stages

- **Stage A (default) — zero infra.** `pip install` + `uvicorn`: UI, sync + async,
  in-memory state, in-process priority scheduler, local `tiny` backend, ffmpeg
  bundled via pip.
- **Stage B (opt-in) — `REDIS_URL` set.** Async goes onto a shared Redis queue
  drained by separate `worker` processes; state/lineage move to Redis. Sync stays
  in-process.

The seam is the `StateStore` interface (+ a Redis queue), with an in-memory
implementation (default) and a Redis one (opt-in). Nothing else knows the difference.

## 2. Stack

- **Python 3.12**, **FastAPI** + **uvicorn** (async).
- **Chunking:** ffmpeg via **`imageio-ffmpeg`** (bundled binary; system ffmpeg used
  if present).
- **Local backend:** `transformers` + `torch`, `whisper-tiny`, model loaded once
  per process and shared, inference on a priority-scheduled thread pool.
- **Hosted backend:** direct `httpx` POST to the HF router (`whisper-large-v3`) —
  not `huggingface_hub` (D18).
- **SSE:** `sse-starlette`. **Config:** `pydantic-settings`. **Logs:** `structlog`
  JSON to stdout (flushed per line), keyed by `job_id`/`chunk_id`/`worker_id`.
- **Stage B:** `redis` (async client). **Tests:** `pytest`, `pytest-asyncio`,
  `jiwer` (WER), `fakeredis` (async-path test).
- **Frontend:** static HTML + **Tailwind via Play CDN** (no Node build step, D-note
  in README) + vanilla JS.

## 3. Repository layout

```
app/
  config.py            # env Settings: tokens, model ids, chunk size, concurrency, REDIS_URL
  main.py              # FastAPI app + lifespan; wires in-memory or Redis store/queue; mounts web/
  auth.py              # static-token dependency (bearer / X-API-Key)
  logging.py           # structlog JSON, per-line flush
  models.py            # Job, Chunk, enums (Status, Mode, Tier, Backend)
  model_server.py      # optional standalone inference server (loads the model once)
  api/
    routes_jobs.py     # POST /jobs, GET /jobs/{id}, /{id}/chunks, /{id}/stream
    routes_health.py   # /healthz
  core/
    chunking.py        # ffmpeg decode → fixed windows + overlap
    reassembly.py      # in-order buffer, contiguous-prefix, overlap stitch
    scheduler.py       # in-process priority queue + N worker coroutines (admission cap)
    pipeline.py        # orchestration: sync (SSE) + async (in-process or Redis)
  backends/
    base.py local.py hosted.py factory.py    # interface, local, HF router, router+fallback
    model_server.py                          # thin HTTP client to a shared inference server
  store/
    base.py memory.py redis_store.py          # interface, in-memory, Redis store + RedisQueue
  worker/
    worker.py          # Stage B: drains Redis queue → transcribe → lineage → finalise
web/
  index.html  app.js   # multi-file upload, per-file result cards, live lineage (Tailwind CDN)
tests/
  unit/                # chunking, reassembly, auth, pipeline (mocked), redis async (fakeredis)
  e2e/                 # WER test on a real clip + fixtures/
pyproject.toml  .env.example  README.md
```

Fully present: `Dockerfile`, `docker-compose.yml`, `.github/workflows/`.

## 4. Domain model (schemas)

```python
Status = pending | processing | done | failed
Mode   = sync | async
Tier   = fast (→ whisper-tiny, local) | strong (→ whisper-large-v3, hosted)
Backend = local | hosted

Chunk: chunk_id, job_id, index, start_ms, end_ms, overlap_ms,
       tier, backend_requested, backend_used, worker_id, priority,
       status, retries, enqueued_at, started_at, finished_at,
       duration_ms (computed), text, error

Job:   job_id, created_at, status, mode, tier, backend_requested,
       source_filename, duration_ms, chunk_count, transcript, error
```

**Redis keys (Stage B):** `job:{id}`, `job:{id}:chunks` (list), `chunk:{id}`,
`chunk:{id}:audio` (raw float32 bytes), `queue:chunks` (list), `job:{id}:remaining`
(counter). The in-memory store mirrors the same shape.

## 5. Endpoints

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/jobs` | multipart `file` + `tier`,`mode`,`backend`. Sync → SSE; async → `{job_id}`. |
| `GET`  | `/jobs/{id}` | status + partial/final transcript |
| `GET`  | `/jobs/{id}/chunks` | full per-chunk lineage |
| `GET`  | `/jobs/{id}/stream` | SSE tail of progress (in-order) |
| `GET`  | `/healthz` | liveness/readiness |
| `GET`  | `/` (+ assets) | UI |

Auth (when `API_TOKEN` set) on all except `/healthz`, `/`, and static assets.
Multi-file upload = the browser issues one `POST /jobs` per file.

## 6. Configuration (env, prefix `TRANSCRIPTOR_`)

| Var | Default | Meaning |
|-----|---------|---------|
| `MODEL_FAST` / `MODEL_STRONG` | `whisper-tiny` / `whisper-large-v3` | local / hosted model |
| `CHUNK_SECONDS` / `CHUNK_OVERLAP_SECONDS` | `20` / `2` | chunk geometry |
| `MAX_CONCURRENT_CHUNKS` | `4` | in-flight cap = local thread-pool size |
| `HOSTED_MAX_CONCURRENCY` | `4` | concurrent HF calls (429 guard) |
| `FALLBACK_TO_LOCAL` | `true` | hosted-fast failure → local |
| `HF_TOKEN` | — | enables hosted (costs HF credits) |
| `REDIS_URL` | — | enables Redis async + workers |
| `MODEL_SERVER_URL` | — | delegate local inference to a shared model-server (model loaded once) |
| `API_TOKEN` | — | enables auth |
| `MAX_UPLOAD_MB` | `200` | upload size cap |

## 7. Status

**Built and verified:**
- Stage A end-to-end — 13 unit tests green; real local WER e2e; live SSE streaming
  + live lineage in the browser.
- Hosted backend (large-v3 via HF router) — verified live (until HF credits ran out;
  fallback to local then kicks in for the fast tier).
- Stage B async — Redis store + queue + worker process, verified with `fakeredis`
  (shared server simulating API + worker), plus the run recipe in the README. A live
  multi-process demo needs a real Redis (WSL `apt install redis-server`, or Memurai).
- Shared inference server (D21) — optional `model-server` container; `MODEL_SERVER_URL`
  makes api/workers thin HTTP clients so the model loads once (not per worker).
  Verified locally end-to-end (server warms `tiny`, the HTTP backend transcribes a
  real clip correctly); wired into `docker-compose` behind a healthcheck.
- Dockerization — `Dockerfile` + `docker-compose` (including a Redis healthcheck, automatic volume caching, and horizontal `--scale worker=N` capability) fully built and verified.
- CI/CD — GitHub Actions workflow (`ci.yml`) fully configured to run code linting (Ruff) and fast unit testing.

**Deferred (the DevOps tail):** Splunk HEC log shipping. This slots in behind existing interfaces without reshaping the app.
