# Transcriptor — Decision Log

> Every meaningful design/implementation decision, with the *why* and the
> alternatives rejected — so each choice can be explained and defended. Detail
> lives in [DESIGN.md](./DESIGN.md) and [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md);
> this is the skimmable rationale. Newest decisions may refine earlier ones.

---

### D1 — The chunk is the core unit (map-reduce)
**Decision:** Split media into fixed time chunks, transcribe each independently,
reassemble in order. In prose the chunk is nicknamed the *atom* (mental model);
**in code and logs it is always `chunk` / `chunk_id`** — no "atom" in the codebase.
**Why:** independent units give parallelism, per-unit traceability, and a clean
map→reduce shape that mirrors the target stack (Spark/Airflow).
**Rejected:** whole-file single call — no parallelism, no partial results, no
per-piece lineage.

### D2 — Two delivery modes: sync (real-time) vs async (queued)
**Decision:** Sync streams the transcript live; async returns a `job_id` the client
polls.
**Why:** different jobs want different things — interactive "watch it happen" vs
long/batch throughput that survives a disconnect.
**Rejected:** one mode for both — either blocks on long files or over-complicates
short interactive ones.

### D3 — In-order streaming via contiguous-prefix buffer
**Decision:** Chunks finish out of order; the server emits only the longest
*contiguous completed prefix* over SSE. Chunk boundaries use a small overlap and
are stitched.
**Why:** the user sees the transcript grow left-to-right with no gaps or
out-of-order text; overlap protects word accuracy at seams.
**Rejected:** show chunks as they finish (jumbled order); hard cuts (slice words).

### D4 — Model↔backend matrix: local `tiny`; remote `whisper-large-v3`
**Decision:** `local → whisper-tiny` (on-box, ~150MB), `remote → whisper-large-v3`
(HF hosted). The hard rule stands: **strong ⇒ remote**; local never loads more than
tiny. Any hosted request uses large-v3 regardless of the tier picked.
*(Supersedes the earlier tiny/small tiers — see below for why.)*
**Why:** local stays tiny so it runs on a constrained box and keeps secret audio
on-box; remote gives the strong model. **Reality forced the remote choice:** HF's
free serverless (`hf-inference`) tier only serves the larger Whisper for ASR —
`tiny`/`small` return "Model not supported" — so remote must be large-v3. See D18.
**Rejected:** remote `tiny`/`small` (not hosted on the free tier); heavy local
models (RAM/load cost).

### D5 — Pluggable backend interface (local / hosted) with fallback
**Decision:** One `TranscriptionBackend` interface, two implementations, selectable
per request; hosted→local fallback is configurable.
**Why:** the orchestration (chunking, queue, lineage) is identical regardless of
where inference runs; local is the data-locality answer for sensitive audio, hosted
is the cheap-demo path. The backend also decides the concurrency primitive (D6).
**Rejected:** hard-coding one backend — loses the local/secret story or the
no-download demo path.

### D6 — Shared single model + thread pool; honest about single-box limits
**Decision:** Model loaded **once per process, shared** across a `ThreadPoolExecutor`
(default size 2). Local inference runs in the pool to keep it off the async loop.
**Why:** memory = one model, not one-per-thread. Torch is *greedy but preemptible*
(uses all cores for one inference, but the OS still time-slices), so a second
concurrent inference doesn't raise throughput — real throughput scaling is
multi-pod (D8). The pool is for **responsiveness + delivery latency**, not
single-box speed.
**Rejected:** one model per thread (RAM blowup); pool size 1 / pure sequential
(a short chunk needlessly waits behind a long one); process pool by default
(more RAM/IPC than a dev box needs).

### D7 — Priority lanes (sync preempts async)
**Decision:** Scheduling is priority-aware: sync (interactive) chunks preempt async
(batch) chunks; shorter chunks may be favoured. Within one sync job, chunks still
process in index order (the UI shows the contiguous prefix).
**Why:** an interactive user shouldn't wait behind a large background job; short
units should drain quickly. This is the fairness/latency lever on a core-bound box.
**Rejected:** FIFO only — interactive work starves behind batch work.

### D8 — Shared state/queue behind interfaces: in-memory → Redis
**Decision:** `StateStore` and `Queue` interfaces with an **in-memory impl (Stage A,
default)** and a **Redis impl (Stage B)**. The Redis queue is Redis-native
(`LPUSH`/`BRPOP`), not Celery.
**Why:** dev runs with zero infra; scale swaps the impl with no other code change.
Cross-pod lineage ("which worker processed this chunk") only means something with
shared state. Redis-native keeps the queue transparent for lineage and light for a
take-home.
**Rejected:** Celery (heavier, more magic, less transparent); in-process only
(no real horizontal scale or cross-pod lineage).

### D9 — Dev-first, two-stage build
**Decision:** Build Stage A (UI + sync, in-memory, no Docker/Redis) first and stop
at the browser milestone for review; add Stage B (Redis/async/worker/Docker) after.
**Why:** the reviewer's machine is 16GB and Docker is heavy there; deliver something
runnable with `pip install` + `uvicorn` early, defer heavy infra.
**Rejected:** infra-first — nothing runnable until late, heavy on the dev box.

### D10 — Structured JSON logs now; Splunk filtering later
**Decision:** `structlog` → JSON to stdout, every line keyed by
`job_id` / `chunk_id` / `worker_id`. Splunk-ingestible as-is; an optional Splunk
**HEC** handler is env-gated and off by default.
**Why:** the target org uses Splunk; JSON with stable keys gives `job_id=… chunk_id=…`
filtering for free. Shipping/filtering is a production concern, not a Stage A one.
**Rejected:** plain text logs (not machine-filterable); wiring Splunk into dev
(infra the reviewer won't run).

### D11 — Lightweight static-token auth, not user management
**Decision:** A single static API token (bearer / `X-API-Key`) via env, checked by
middleware; no accounts/login/roles.
**Why:** demonstrates access-control thinking without scope creep. README notes the
path to real auth (OIDC/mTLS, K8s Secrets/Vault).
**Rejected:** full user system (out of scope); no auth (ignores security).

### D12 — WER-based e2e test with a real recording
**Decision:** A self-recorded clip transcribed through the real pipeline, asserted
against expected text via a **word-error-rate threshold**. Fast unit tests mock the
model so CI needs no weights/Docker.
**Why:** ASR is never character-exact, so a fuzzy criterion is the correct one; unit
tests stay deterministic and fast.
**Rejected:** exact-match assertion (flaky); only manual testing (not repeatable).

### D13 — ffmpeg via `imageio-ffmpeg`
**Decision:** Bundle ffmpeg through the pip package `imageio-ffmpeg`; use a system
ffmpeg automatically if present.
**Why:** `pip install` is enough — no system-wide ffmpeg install on the dev box.
**Rejected:** require system ffmpeg (extra setup friction).

### D14 — Fixed-window chunking with overlap (VAD deferred)
**Decision:** 20s windows with 2s overlap, stitched.
**Why:** deterministic and easy to reason about for lineage; overlap protects seam
accuracy; 20s trades a little accuracy vs Whisper's 30s window for faster first
output and more parallelism.
**Rejected (for now):** silence/VAD-aware splitting — more elegant, more complexity;
noted as a future improvement.

### D15 — Docs live in `docs/`, with this decision log
**Decision:** Design, plan, and this decision log live under `docs/`. Pure
"how does X work" questions are answered in chat and **not** added to docs; only
design/implementation *decisions* are recorded here.
**Why:** the decisions must be explainable later; keep the docs signal-dense.

### D16 — Admission control / rate limiting, separate from cross-pod balancing
**Decision:** A configurable `MAX_CONCURRENT_CHUNKS` semaphore bounds in-flight
chunks per pod (local: the thread-pool bound; hosted: a concurrent-request cap that
also rate-limits against HF 429s). Excess work waits in the queue (backpressure).
Cross-pod distribution is a *separate* infrastructure layer — a load balancer / K8s
Service in front of worker pods.
**Why:** protects CPU/RAM from overcommit and external APIs from rate limits, with
predictable load; the queue absorbs bursts instead of the CPU. Naming the two
layers (in-pod admission vs cross-pod balancing) keeps the scaling story precise.
**Rejected:** unbounded concurrency (thrash / OOM / 429 storms); relying only on a
load balancer (doesn't protect a single pod's resources).

### D17 — Stack chosen to mirror the target role
**Decision:** Python + FastAPI + Redis + `transformers`/HF, Docker/compose, GitHub
Actions.
**Why:** matches the job's stack and lets the design map cleanly onto
Spark (map-reduce), Airflow (DAG of chunk tasks), K8s (stateless replicas), CI/CD.
**Rejected:** a Node backend calling a Python worker — extra moving part for no gain
given the model is Python.

### D18 — Hosted backend calls the HF router directly over HTTP
**Decision:** The hosted backend POSTs FLAC audio straight to
`https://router.huggingface.co/hf-inference/models/<model>` with an explicit
`Content-Type`, instead of using `huggingface_hub`'s high-level async client.
**Why:** in `huggingface_hub` 1.x the default `provider="auto"` fails to resolve a
provider for Whisper models (raises `StopIteration`), and the client omits the
request Content-Type for raw audio (HF replies `Content type "None" not supported`).
A direct POST is a few lines, avoids that fragility, and gives explicit control of
the endpoint, content type, and 503-warmup retries. Also: the old
`api-inference.huggingface.co` host no longer resolves — the router URL is current.
**Rejected:** `huggingface_hub` high-level client (broken for this task in 1.x);
per-request temp files just to coax the client into setting a content type.

### D19 — Redis async: shared queue + worker processes; sync stays in-process
**Decision (Stage B):** When `REDIS_URL` is set, **async** jobs are enqueued on a
Redis list (`rpush`/`blpop`, FIFO) and drained by separate `python -m app.worker.worker`
processes; job/chunk state + lineage live in Redis behind the same `StateStore`.
**Sync stays in-process** (it needs the real-time SSE stream). Specifics:
- Each chunk's audio is parked in Redis as raw float32 bytes (shortcut; production
  → object storage + a reference).
- The worker **recomputes the backend plan** from the chunk's `tier`/`backend`
  rather than shipping it through the queue — deterministic given the same settings.
- A per-job `remaining` counter (`decr`) lets the worker that finishes the last
  chunk assemble the transcript and finalise — no coordinator needed.
- `worker_id = host/pid/consumer` so lineage shows which process handled each chunk;
  run more worker processes to fan chunks out (the local, no-Docker stand-in for pods).
**Why:** delivers the "chunks to different workers" scaling story on one machine
without Docker; the in-memory path stays the zero-infra default.
**Rejected:** Celery (heavier/opaque); a priority ZSET queue (deferred — sync, the
thing that needs priority, doesn't use this queue); a dedicated coordinator process
(the remaining-counter makes finalisation self-organising).

### D20 — Between-job scheduling is arrival-order backfill; fair-share deferred
**Decision:** When multiple files are submitted, their chunks share the worker pool
ordered by `(priority, job-arrival, chunk-index)`. Workers stay fully utilised — an
earlier job fills them and later jobs **backfill the instant a worker frees** (no
idle time) — but ordering is not fair-share: an earlier job's chunks all outrank a
later one's. A round-robin / preemptive fair-share scheduler between jobs (so files
visibly progress together) was **considered and deliberately deferred**.
**Why:** measured behaviour is already 100%-utilised and correct (verified: a second
file's chunks started the exact ms the first file's workers freed). Fair-share is
real work for little take-home value; on one box it doesn't reduce total time
(core-bound), and genuine throughput comes from adding workers, not re-slicing a
saturated pool. Noted as future work rather than built.
**Rejected (for now):** round-robin/weighted-fair-queuing across jobs; per-job
worker reservations — both add scheduler complexity without changing wall-clock on a
single machine.

### D21 — Optional shared inference server (one model in RAM, not one per worker)
**Decision:** In-process inference loads the model into every process (`api` +
each worker). Setting `MODEL_SERVER_URL` makes the "local" backend delegate to a
shared **model-server** (`app/model_server.py`, its own container) over HTTP, so the
model is loaded **once** and api/workers become thin clients. Selected by the
factory exactly like the in-memory↔Redis swap; the model-server internally *reuses*
`LocalBackend`, so behaviour and lineage are identical.
**Why:** per-worker loading is fine for `tiny` (~150MB) at demo scale, but it
multiplies with worker count and collapses for a big model or a GPU (you can't
replicate weights across VRAM, and one server can dynamically batch). This is the
production pattern (Triton/vLLM/TorchServe) — and the `TranscriptionBackend`
interface already anticipated it: a model-server is just a self-hosted `hosted`
backend (internal URL instead of the HF router). The three tiers form one spectrum
through one interface: **in-process → self-hosted model-server → HF hosted**.
**Rejected:** baking a model server into every deployment (unneeded for the local
default); pouring Triton/vLLM into a take-home (a ~50-line FastAPI server reusing
`LocalBackend` proves the pattern without the weight).
