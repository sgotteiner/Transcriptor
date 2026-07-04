# Transcriptor — Decision Log

The decisions worth defending, each with the *why* and the alternative rejected.
Architecture detail: [DESIGN.md](./DESIGN.md).

---

**D1 — Chunk is the unit (map-reduce).** Split media into fixed time chunks,
transcribe each independently, reassemble in order; the chunk is both the work-unit
and the lineage-unit (called `chunk` in code; "atom" is prose only).
*Why:* parallelism, per-piece traceability, and a Spark/Airflow-shaped pipeline.
*Rejected:* whole-file single call — no parallelism, partials, or per-piece lineage.

**D2 — Real-time (sync) vs batch (async).** Sync streams the transcript live over
SSE as chunks finish; async runs the job in the background and is polled (in-process,
or a shared Redis queue drained by separate worker processes when `REDIS_URL` is
set). Both surface a `job_id` — sync emits it up front so the UI can pull live
lineage. *Why:* interactive real-time output vs decoupled batch throughput that
isn't bound to one request. *Rejected:* a single mode — either blocks on long files
or over-serves short interactive ones.

**D3 — Models: local `tiny`, remote `whisper-large-v3` (strong ⇒ remote).** Local
never loads more than tiny; any hosted request uses large-v3. *Why:* local stays
tiny for a constrained box and keeps secret audio on-box; **reality forced remote =
large-v3** — HF's free serverless only serves the large Whisper for ASR (tiny/small
return "Model not supported"). *Rejected:* remote tiny/small (not hosted free); heavy
local models (RAM/load cost).

**D4 — Match the concurrency primitive to the work.** Coordination (uploads,
queueing, SSE, polling, HTTP calls to HF) is I/O-bound → **asyncio**; local model
inference is CPU-bound → a **thread pool** (kept off the event loop; the model is
loaded once per process and shared); hosted inference is a network call →
**coroutines**, no pool. *Why:* one process juggles many jobs without blocking, and
CPU work never freezes the event loop. Honest limit: Torch is *greedy but
preemptible* (one inference already uses all cores), so a second concurrent local
inference adds no throughput — the pool is for responsiveness/latency; real
throughput comes from more worker processes. *Rejected:* async-only (CPU inference
freezes the loop); a thread/process per request (RAM blowup, no gain on a core-bound
box).

**D5 — Priority lanes (sync preempts async).** Sync (interactive) chunks preempt
async (batch) chunks; within a job, strict index order. *Why:* interactive work
shouldn't starve behind a large batch job — the fairness/latency lever on a
core-bound box. *Rejected:* FIFO only (interactive work starves).

**D6 — Dev-first, two-stage build.** Stage A (UI + sync, in-memory, no infra) first;
Stage B (Redis/async/workers/Docker) after. *Why:* deliver something runnable with
`pip install` + `uvicorn` early, defer heavy infra on a 16GB box. *Rejected:*
infra-first (nothing runnable until late).

**D7 — Structured JSON logs (Splunk-ready).** structlog → JSON to stdout keyed by
`job_id`/`chunk_id`/`worker_id`; optional Splunk HEC handler, off by default. *Why:*
the target org uses Splunk; stable keys give `job_id=… chunk_id=…` filtering for
free; shipping is a production concern, not Stage A. *Rejected:* plain-text logs (not
filterable); wiring Splunk into dev.

**D8 — Lightweight static-token auth.** Single API token (bearer / `X-API-Key`) via
env + middleware; no accounts/roles. *Why:* shows access-control thinking without
scope creep; README notes the path to OIDC/mTLS + K8s Secrets/Vault. *Rejected:* full
user system (out of scope); no auth (ignores security).

**D9 — WER e2e test + mocked units.** A real clip through the real pipeline, asserted
by a word-error-rate threshold; unit tests mock the model (no weights/Docker in CI).
*Why:* ASR is never character-exact, so a fuzzy criterion is correct; unit tests stay
deterministic and fast. *Rejected:* exact-match (flaky); manual-only (not
repeatable).

**D10 — Fixed-window chunking with overlap.** 20s windows, 2s overlap, stitched.
*Why:* deterministic (good for lineage); overlap protects word accuracy at seams; 20s
(vs Whisper's 30s) trades a little accuracy for faster first output and more
parallelism. *Rejected (for now):* VAD/silence-aware splitting — more elegant, more
complex; a noted future improvement.

**D11 — Admission control ≠ cross-pod balancing.** A `MAX_CONCURRENT_CHUNKS`
semaphore bounds in-flight chunks per process (local: the pool bound; hosted: a
concurrent-call cap that also guards HF 429s); excess queues (backpressure).
Distributing across workers is a *separate* layer (load balancer / K8s Service).
*Why:* protects CPU/RAM and external APIs; naming the two layers keeps the scaling
story precise. *Rejected:* unbounded concurrency (thrash/OOM/429 storms); relying on a
load balancer alone (doesn't protect a single process's resources).

**D12 — Between-job scheduling = arrival-order backfill.** Multiple files share the
worker pool ordered by `(priority, job-arrival, index)`; workers stay 100% utilised
(a later file's chunks start the instant a worker frees) but it's not fair-share.
*Why:* measured behaviour is already fully utilised and correct; fair-share is real
work for no wall-clock gain on one box (core-bound) — throughput comes from adding
workers. *Rejected (for now):* round-robin / weighted-fair-queuing; per-job worker
reservations — complexity with no single-machine speedup.

**D13 — Optional shared inference server (one model in RAM).** `MODEL_SERVER_URL` set
⇒ the local backend delegates to a shared **model-server** over HTTP (model loaded
once; api/workers become thin clients); the server reuses the local backend so
behaviour and lineage are identical. *Why:* per-worker loading is fine for tiny but
multiplies with worker count and collapses for a big model or a GPU (can't replicate
weights across VRAM; one server can batch) — the Triton/vLLM pattern, and the backend
interface already anticipated it (a model-server is just a self-hosted hosted
backend). One spectrum, one interface: **in-process → self-hosted model-server → HF
hosted.** *Rejected:* a model server in every deployment (unneeded for the local
default); Triton/vLLM in a take-home (a ~50-line server reusing the local backend
proves the pattern).
