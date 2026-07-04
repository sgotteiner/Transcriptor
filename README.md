# Transcriptor

Upload a media file, get a transcription. The audio is split into **chunks**
(map), transcribed independently (fan-out across a priority-scheduled pool),
and reassembled **in order** (reduce) — streamed live for sync jobs, polled for
async jobs. Every chunk carries **lineage**: which backend and worker handled it,
timings, retries, and outcome.

Powered by Hugging Face Whisper — `tiny` locally (on-box, zero-config) or the
Inference API for a stronger model.

> Design rationale, decision log, and the feature/technical breakdown live in
> [`docs/`](./docs) (`DESIGN.md`, `IMPLEMENTATION_PLAN.md`, `DECISIONS.md`,
> `SUMMARY.md`).

> [!TIP]
> For a clean, minimal list of just the setup commands, flags, and configurations, see [`setup.md`](./setup.md).

---

## Run it (Stage A — no Docker, no Redis)

Requires **Python 3.11 or 3.12**. ffmpeg is bundled via pip — nothing to install.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -e .
uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000** → pick a file → **Transcribe**.

The first local run downloads `whisper-tiny` (~150 MB) once. No token or account
is needed for the local backend.

### The four controls
- **Model** — *fast* (`whisper-tiny`, local) or *strong* (`whisper-large-v3`, remote-only).
- **Backend** — *local* (audio stays on-box) or *remote* (Hugging Face).
- **Mode** — *sync* (watch the transcript stream in order) or *async* (queued, polled).
- **API token** — only if you enabled auth (see below).

The UI enforces the rule **strong ⇒ remote** (local only ever runs `tiny`).

---

## Using the stronger (remote) model

Create a free token at <https://huggingface.co/settings/tokens>, then:

```bash
cp .env.example .env
# set TRANSCRIPTOR_HF_TOKEN=hf_xxx  in .env
```

Now *Strong* / *Remote* options work. If a hosted call fails and fallback is on,
fast-tier jobs fall back to local automatically (recorded in each chunk's lineage).

---

## API

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/jobs` | Upload (`file`, `tier`, `mode`, `backend`). Sync → SSE stream; async → `{job_id}`. |
| `GET`  | `/jobs/{id}` | Status + partial/final transcript. |
| `GET`  | `/jobs/{id}/chunks` | Per-chunk lineage (traceability). |
| `GET`  | `/jobs/{id}/stream` | SSE tail of progress (handy for async). |
| `GET`  | `/healthz` | Liveness/readiness. |

Example (async):

```bash
curl -F file=@clip.mp3 -F mode=async -F tier=fast -F backend=local \
     http://127.0.0.1:8000/jobs
curl http://127.0.0.1:8000/jobs/<job_id>
curl http://127.0.0.1:8000/jobs/<job_id>/chunks
```

### Auth (optional)
Set `TRANSCRIPTOR_API_TOKEN` to protect the API; send it as
`Authorization: Bearer <token>` or `X-API-Key: <token>`. Empty = auth disabled.

---

## Tests

```bash
pip install -e ".[dev]"
pytest                 # fast unit tests — mocked model, no weights, no network
pytest -m e2e          # real transcription (see below)
```

**End-to-end with your own recording:** drop a clip at
`tests/e2e/fixtures/sample.<ext>` and its text at
`tests/e2e/fixtures/expected.txt`. `pytest -m e2e` transcribes it through the real
local pipeline and asserts a word-error-rate below threshold (ASR is never
character-exact).

---

## Async at scale (Stage B — Redis & Workers)

By default async runs in-process. When connected to Redis, async jobs go onto a **shared queue** drained by **separate worker processes** (or worker containers). State and per-chunk lineage live in Redis, allowing horizontal scale. Sync mode stays in-process (it needs the real-time stream).

### Option A: Run with Docker Compose (Recommended)
This is the easiest way to test Stage B. It spins up four services — Redis, a
**model-server** (loads the model once), the API, and workers — automatically. The
API and workers are thin clients that call the model-server, so scaling workers does
**not** multiply model copies in RAM.

```bash
# Start Redis, model-server, API, and a worker
docker compose up --build

# Scale to multiple workers to observe horizontal chunk load balancing
# (all workers share the one model-server — RAM stays flat)
docker compose up --build --scale worker=3
```

Visit **http://127.0.0.1:8000** and submit a job in **Async · queued** mode. You will see different worker containers balance the chunks. Model weights are cached locally in a volume so they survive container restarts.

---

### Option B: Run natively (no Docker)

**1. Run Redis natively:**
- **WSL:** `sudo apt install redis-server && redis-server` (reachable from Windows at `localhost:6379`), or
- **Windows:** [Memurai](https://www.memurai.com/) (a native Redis-compatible service).

**2. Point the app at it** — in `.env`:
```env
TRANSCRIPTOR_REDIS_URL=redis://localhost:6379/0
```

**3. Start the API and workers in separate terminals:**
```bash
uvicorn app.main:app --port 8000
python -m app.worker.worker      # Run this in N terminals to scale out workers
```

**4. Check progress:**
In the UI, select **Async · queued**. Chunks will be spread across the host/pid worker processes, as shown in the lineage audit table.

*(Note: Chunk audio is parked in Redis as raw bytes for local ease; production systems would write to object storage like S3/MinIO and store reference links in Redis.)*

---

## Configuration

All via env (or `.env`), prefix `TRANSCRIPTOR_`. Full annotated list in
[`.env.example`](./.env.example). Highlights:

| Var | Default | Meaning |
|-----|---------|---------|
| `MODEL_FAST` / `MODEL_STRONG` | `whisper-tiny` / `whisper-large-v3` | local / hosted model ids |
| `CHUNK_SECONDS` / `CHUNK_OVERLAP_SECONDS` | `20` / `2` | chunk geometry |
| `MAX_CONCURRENT_CHUNKS` | `4` | admission cap (chunks in flight per process) |
| `HOSTED_MAX_CONCURRENCY` | `4` | concurrent HF calls (429 guard) |
| `HF_TOKEN` | — | enables the remote backend |
| `REDIS_URL` | — | enables Redis async + workers (else in-process) |
| `MODEL_SERVER_URL` | — | delegate local inference to a shared model-server (loaded once) |
| `API_TOKEN` | — | enables auth |

---

## Status & scope

**Stage A** (default) runs with `pip install` + `uvicorn`: in-memory state,
in-process priority scheduler, async handled in-process. **Stage B** adds the
Redis-backed shared queue + separate worker processes, an optional shared
**model-server** (one model in RAM for all workers), Docker/compose (with
`--scale worker=N`), and GitHub Actions CI (lint + tests) — all behind the same
interfaces, no app reshaping. Only **Splunk HEC log shipping** is deliberately
deferred (structured JSON logs are already Splunk-ingestible; see `DECISIONS.md`).

### Assumptions & shortcuts (called out honestly)
- **In-memory state by default** — lost on restart; set `REDIS_URL` for shared,
  worker-drained state (chunk audio parked in Redis; production → object storage).
- **Single static token**, not a user system.
- **CPU-first**, `tiny` locally; a GPU would be a config change, not a redesign.
- **Frontend uses Tailwind via the Play CDN** (no Node build step, to keep the repo
  Python-only and light). Production would precompile with the Tailwind CLI and
  serve a static, purged stylesheet instead of the runtime CDN.
- **Overlap-based chunk stitching** is pragmatic, not linguistically optimal
  (VAD-aware splitting is a noted future improvement).
- The whole job's decoded audio is held in memory — fine for typical files; a
  streaming/disk-backed path would be the production hardening.
