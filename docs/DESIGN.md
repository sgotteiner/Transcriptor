# Transcriptor — System Design

Upload a media file, get a transcription — built to production-grade backend
standards. This doc is the **what and why**; exact schemas, endpoints, module
layout and run steps are in [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)
(as-built) and the rationale/alternatives for each choice are in
[DECISIONS.md](./DECISIONS.md) (referenced as `D1`…`D20`). One-page skim:
[SUMMARY.md](./SUMMARY.md).

---

## 1. Scope

Upload → chunk → transcribe (Hugging Face Whisper) → reassemble in order, with
real-time streaming or queued processing, per-chunk traceability, two backends
(local / hosted), and lightweight auth.

**Out of scope:** user accounts, a durable DB, billing, a rich SPA, and running
Spark/Airflow themselves (see §11 for how the design *maps onto* that stack).

## 2. Domain context (why the choices lean this way)

A backend role in a defence / intelligence-data setting (Python, Docker, K8s,
Airflow, Spark, CI/CD). Three consequences:

- **Data locality** — intelligence audio usually can't leave the perimeter, so a
  **local, on-box inference path is the realistic answer**; hosted is the cheap
  demo path. The UI makes it an explicit choice.
- **Lineage** — these pipelines care *what happened to each piece of data, where,
  and when*. That is the per-chunk traceability below, and the product's spine.
- **Batch/parallel is native** — chunk → transcribe → reduce is map-reduce, which
  maps cleanly onto Spark/Airflow/K8s.

## 3. Core concept: the chunk (and its lineage)

The **chunk** is the indivisible unit — one time-slice of media. It is at once the
**unit of work** (one transcription call) and the **unit of observability** (one
lineage record). A **job** is one uploaded file: its set of chunks plus the
reassembled transcript. (In prose we sometimes call a chunk an *atom* and a job a
*molecule* — a mental model only; the code and logs always say `chunk`/`job`.)

```
file ──split──▶ [chunk₀ chunk₁ chunk₂ …]   (map: independent slices)
                    │     │     │
                transcribe each             (fan out across threads / workers)
                    │     │     │
                reassemble in time order ──▶ transcript   (reduce)
```

Chunks are independent, so they transcribe in any order, on any worker — which is
what enables both parallelism and traceability. Every chunk carries a **lineage
record**: identity (`chunk_id`, `job_id`, `index`), span (`start_ms`/`end_ms` +
overlap), how (`tier`, `backend_used`), where (`worker_id`), when
(`started/finished`, derived `duration_ms`), and outcome (`status`, `retries`,
`text`). Exact schema: IMPLEMENTATION_PLAN §4.

## 4. Architecture

```
 Browser (UI)         FastAPI (async)                    Transcription backends
 multi-file      ┌─────────────────────────┐            ┌── local  (Whisper, CPU,
 upload + knobs  │ auth · split · reassemble│───────────┤          thread pool)
 ───SSE/poll───▶ │ SYNC → stream (SSE)      │  transcribe└── hosted (HF router,
                 │ ASYNC → enqueue → job_id │                       async HTTP)
                 └───────┬─────────────────┬┘
        in-process       │                 │        Stage B (REDIS_URL set):
        scheduler ◀──────┘                 └──────▶ Redis queue + state, drained
        (default)                                   by separate worker processes
```

- **API service (FastAPI, async)** — auth, upload, chunking, in-order reassembly,
  SSE streaming, and read endpoints for status / transcript / lineage.
- **Sync** runs on an **in-process priority scheduler** (it needs the live stream).
- **Async** runs in-process by default; with `REDIS_URL` set it becomes a **shared
  Redis queue drained by separate `worker` processes**, with job/chunk state and
  lineage in Redis so any worker handles any chunk (D19). This is what makes
  "which worker processed this chunk" a real cross-process fact and the local,
  no-Docker stand-in for K8s worker pods.
- **State store** and **queue** sit behind interfaces: in-memory (default) ↔ Redis
  (opt-in), swappable without touching the rest of the app.

## 5. Sync vs async

Both chunk and reduce in time order; they differ in *delivery*.

- **Sync — real-time, in order.** Chunks finish out of order, but an **in-order
  reassembly buffer** emits only the longest contiguous completed prefix, streamed
  via **SSE** — so the transcript types out left-to-right with no gaps. Best for
  interactive "watch it happen".
- **Async — queued.** Upload returns a `job_id`; the client polls
  `GET /jobs/{id}`. Best for long/batch work and surviving a disconnect.

## 6. Concurrency: the IO-vs-compute seam (D6, D7)

Rule: **never sit idle, but parallelise the right thing with the right mechanism.**

- **Coordination is IO-bound** (uploads, queueing, Redis, SSE, polling) → asyncio;
  one API process juggles many jobs without blocking.
- **Local inference is compute-bound** → a **thread pool** keeps the blocking call
  off the event loop; the **model loads once per process, shared** across threads.
- **Hosted inference is IO-bound** (HTTP) → coroutines, no thread pool.

**Honest single-box note:** PyTorch is *greedy but preemptible* — one inference
already spreads across the cores, so a second concurrent one doesn't add
throughput; they share cores. Concurrency + ordering still help **delivery latency
and fairness**, so the pool defaults to 2 and dispatch is priority-aware. Real
*throughput* comes from more workers (§4), not more threads.

- **Within a job:** strict chunk order (the reader needs the *next* chunk, not a
  later one that finished early); concurrency there buys pipelining, not reordering.
- **Between jobs:** arrival-ordered backfill — workers stay 100% utilised and a
  later file's chunks start the instant a worker frees, but it's not fair-share. A
  fair-share scheduler was considered and deferred (**D20**).
- **Admission control:** a `MAX_CONCURRENT_CHUNKS` cap bounds in-flight chunks
  (local: the pool size; hosted: a concurrent-call cap that also guards HF 429s);
  excess waits in the queue. Distinct from cross-worker distribution (D16).

## 7. Backends & models (D4, D5, D18)

One `TranscriptionBackend` interface, two implementations, chosen per request:

- **local** → `whisper-tiny`, in-process on CPU (~150MB). On-box, secret-safe, the
  zero-token default.
- **hosted** → `whisper-large-v3` via a direct POST to the HF router
  (`router.huggingface.co/hf-inference`). HF's free tier only serves the large
  Whisper for ASR (tiny/small return "not supported"), so any hosted request uses
  large-v3. Requires an HF token; **uses HF credits**.

Hard rule **strong ⇒ remote** (local never loads more than tiny). If a hosted call
fails (no token, 402/429, offline) and fallback is enabled, the *fast* tier falls
back to local and records `backend_used=local` in the chunk's lineage, so a mixed
job is fully auditable; the strong tier has no local model, so it surfaces the
error instead.

**Scaling local inference (D21):** setting `MODEL_SERVER_URL` swaps the in-process
local backend for a thin HTTP client to a shared **model-server** container, so the
model is loaded once for all workers instead of once per process. The three tiers
then form one spectrum through the same interface:
**in-process → self-hosted model-server → HF hosted.**

## 8. Chunking (D14)

Fixed-size windows with a small left-overlap, stitched by dropping duplicated words
at each seam — deterministic (good for lineage) and boundary-safe. Chunk length is
configurable (the main latency-vs-overhead knob). VAD/silence-aware splitting is a
noted future improvement.

## 9. Traceability, observability & security

- **Lineage view:** `GET /jobs/{id}/chunks` reconstructs every chunk's journey —
  worker, backend, timings, retries, text. This is the product surface, not a
  by-product.
- **Structured JSON logs**, keyed by `job_id`/`chunk_id`/`worker_id`, flushed per
  line, in a shape **Splunk** ingests directly (filter `job_id=… chunk_id=…`). An
  optional Splunk HEC handler is env-gated (D10).
- **Health:** `/healthz` for K8s probes. Metrics (chunks/sec, queue depth) are a
  noted next step.
- **Security (D11):** a static API token (bearer / `X-API-Key`) gates non-public
  endpoints — access control without a user system. Kept distinct from the HF
  token. Generalises to OIDC/mTLS + K8s Secrets/Vault in a real deployment.

## 10. Scalability

- **Within a file:** its chunks fan across workers — faster with more workers.
- **Across files:** chunks share the worker pool, parallel up to the worker count
  with no idle time, then queue (D20).
- **Scale is operational, not code:** add worker processes
  (`python -m app.worker.worker` ×N; K8s pods with a Service/HPA in front).
  Workers are stateless and interchangeable.

## 11. Mapping to the target stack

- **Spark** — chunk → transcribe → reduce *is* map-reduce; the queue is a
  hand-rolled shuffle.
- **Airflow** — a job is a DAG (split → N transcribe tasks → reduce); lineage is
  the task-instance history.
- **Kubernetes** — stateless API + worker deployments, Redis as a backing service,
  `/healthz` probes, scale via replicas.
- **CI/CD** — GitHub Actions running lint + the fast test suite (planned; see
  IMPLEMENTATION_PLAN status).

## 12. Shortcuts & non-goals

Stated honestly (fuller list in the README): in-memory state by default (Redis for
shared/durable-ish); chunk audio parked in Redis as raw bytes (→ object storage in
prod); single static token, no user system; CPU-first (GPU = config, not redesign);
overlap-stitching is pragmatic not linguistically optimal; hosted large-v3 costs HF
credits. Built: `Dockerfile` + `docker-compose` (Redis healthcheck, HF model cache volume, `--scale worker=N`), GitHub Actions CI (CPU-only torch, Ruff lint, pytest). Deferred: Splunk HEC log shipping.
