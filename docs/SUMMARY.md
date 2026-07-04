# Transcriptor — Summary

> A condensed, two-bucket view of the decisions behind the system. Full detail in
> [DESIGN.md](./DESIGN.md), [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md), and
> the rationale/alternatives in [DECISIONS.md](./DECISIONS.md).

---

## Features & Business Decisions (the *what* and *why-for-the-product*)

- **Core product:** upload an audio/video file → get a transcription back, via a
  free Hugging Face (Whisper) model.
- **User picks accuracy vs speed:** *fast* (tiny, local) vs *strong* (large-v3,
  hosted on Hugging Face) — exposed as a choice, not hidden.
- **User picks how they wait:** *sync* mode streams the transcript live (watch it
  type out in order); *async* mode queues the job and is polled — for long/batch
  work that shouldn't block.
- **Batch upload:** drop *multiple files* at once — each becomes its own job and its
  own result card (title, transcript, live per-chunk lineage) in a list.
- **Data-locality as a feature:** *local* backend keeps audio on-box (for
  secret/sensitive material — relevant to the defense/intel context); *remote*
  backend either offloads compute or unlocks higher accuracy.
- **Deliberate model↔backend matrix:** local = `whisper-tiny` (on-box); remote =
  `whisper-large-v3` (HF hosted); choosing strong forces remote. Remote is called
  directly against the HF router (see technical decisions).
- **Real-time, in-order reading experience:** the transcript grows front-to-back
  with no gaps, because the reader needs the *next* piece — not whichever chunk
  happened to finish first.
- **Traceability as a first-class product surface:** every chunk's journey (which
  worker, which backend, timings, retries, outcome) is auditable — an
  observability/lineage story that fits intelligence-data work.
- **Fairness between jobs:** interactive (sync) work takes priority over background
  (batch) work.
- **Security posture without over-building:** a lightweight API token — shows
  access-control thinking, no full user management.
- **Scalability as a product property:** files share the worker pool with no idle
  time (parallel up to the worker count; then they queue); more workers = faster.
  A fair-share scheduler between jobs was considered and deferred (D20).
- **Fits the org's tooling:** logs are structured for Splunk filtering by
  `job_id` / `chunk_id`.

---

## Technical Decisions (the *how*)

- **Python + FastAPI (async)** — matches the target stack; the model layer is
  Python anyway.
- **Chunk is the atomic unit; map-reduce** — split → transcribe independently →
  reduce in time order.
- **Chunking:** fixed 20s windows + 2s overlap, stitched (VAD-based splitting
  deferred).
- **Real-time streaming:** SSE + a *contiguous-prefix reassembly buffer* so
  out-of-order completions still display in order.
- **Concurrency model, placed honestly:** asyncio for IO-bound coordination; a
  shared-model thread pool for local CPU inference; coroutines for hosted (network)
  inference. Torch is *greedy but preemptible*, so on one box concurrency buys
  **latency/fairness**, and **throughput scales via pods**.
- **One model loaded per process, shared across threads** — not one model per
  thread.
- **Priority-aware scheduling:** reorder only *between* jobs (sync preempts async);
  strict index order *within* a job.
- **Admission control / rate limiting:** `MAX_CONCURRENT_CHUNKS` semaphore + a
  hosted concurrent-request cap (guards against HF 429s); queue as backpressure —
  distinct from the cross-pod load balancer.
- **Pluggable backend interface** with configurable fallback — one spectrum through
  one interface: **in-process → shared model-server (one model in RAM) → HF hosted**
  (the model-server is opt-in via `MODEL_SERVER_URL`; keeps RAM flat as workers scale).
- **Shared state + queue behind interfaces:** in-memory (Stage A) → Redis (Stage
  B); Redis-native queue, not Celery.
- **Dev-first, two-stage build:** runnable with `pip install` + `uvicorn`, no
  Docker/Redis, until Stage B adds scale.
- **Structured JSON logs** keyed by `job_id` / `chunk_id` / `worker_id`; optional
  Splunk HEC handler.
- **ffmpeg bundled via `imageio-ffmpeg`** — no system install.
- **Static-token auth** middleware.
- **Testing:** fast mocked unit tests (no weights, CI-friendly) + a WER-threshold
  e2e test from a real recording.
- **One Docker image, two roles** (api/worker) by entrypoint; `--scale worker=N`.
- **Design maps onto Spark (map-reduce) / Airflow (DAG) / K8s (stateless replicas)
  / CI-CD.**
