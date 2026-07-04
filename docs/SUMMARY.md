# Transcriptor — Feature Summary

A one-page list of what the system does. *Why* it's built this way →
[DECISIONS.md](./DECISIONS.md); architecture → [DESIGN.md](./DESIGN.md).

## What it does
- **Upload media → get a transcription** (Hugging Face Whisper), audio *or* video.
- **Batch upload** — drop several files at once; each becomes its own job with its
  own result card (file title, transcript, chunk lineage).
- **Two modes** — *sync* streams the transcript live (SSE) as it's produced; *async*
  queues the job and is polled (for long/batch work).
- **In-order streaming** — chunks finish out of order but the transcript is
  reassembled gapless and left-to-right, with overlap seams stitched.
- **Two model tiers** — *fast* (`whisper-tiny`, local) and *strong*
  (`whisper-large-v3`, hosted), chosen per request.
- **Two backends** — *local* (on-box, secret-safe, zero-config) and *remote*
  (Hugging Face), with automatic fallback and a per-chunk record of which was used.
- **Per-chunk traceability** — every chunk's journey (worker, backend, time span,
  timings, retries, status) is auditable in the UI and in the logs.

## How it scales
- **Chunked map-reduce** — split → transcribe in parallel → reassemble in order.
- **Async at scale** — a shared **Redis** queue drained by separate **worker
  processes**; add workers to go faster (verified: 2 workers = 8 chunks in flight).
- **Docker Compose** — Redis + model-server + API + workers, with `--scale worker=N`.
- **Shared inference server** — one model in RAM for *all* workers (they're thin
  HTTP clients), so scaling workers doesn't multiply model copies.
- **Admission control** — a concurrency cap bounds in-flight chunks per process and
  rate-limits the hosted API.

## Ops & quality
- **Structured JSON logs** keyed by `job_id`/`chunk_id`/`worker_id` (Splunk-ready);
  `/healthz` for K8s; optional **static-token auth**.
- **Tests** — a fast mocked unit suite (deterministic, no weights) plus a real
  word-error-rate end-to-end test; **GitHub Actions CI** runs lint + tests.
- **Runs two ways** — `pip install` + `uvicorn` (no Docker, no Redis) *or* full
  Docker Compose. All behaviour toggled by env vars.
