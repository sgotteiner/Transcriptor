# Transcriptor — Interview Prep Notes

Personal prep cheat-sheet. Reflects the **real** system (as-built), not the polished
design docs. Read before the interview.

## What it is
Upload a media file (audio/video) → get a transcription (Hugging Face Whisper). The
engineering point isn't "call a model" — it's the **architecture**: split the file into
chunks, transcribe them in parallel, reassemble in order, with full per-chunk
traceability, two delivery modes, and pluggable backends.

## Development process (the honest arc — tell this as a story)
1. **Started as a monolith POC.** One process (the API) did *everything*: receive the
   file, chunk it (map), transcribe with an in-process priority scheduler + backpressure,
   reassemble (reduce), and hold state in memory.
2. **Then added Redis + Docker.** Split into a thin API, a **worker** service, and a
   shared **model-server** (inference). Moved the store and the work queue into Redis.
   Reassembly now happens on whichever worker finishes a job's last chunk.
3. **Kept the no-Redis path** as a zero-infra dev/demo mode (`pip install` + run).

Framing: *"It grew from a POC. Under Docker the topology is clean — thin API,
model-server, workers. But the default local mode is still the fat monolith, and there's
cleanup I'd do."*

## The system today — components
- **API** — always chunks the file; serves reads/polls. In **sync** mode it also
  transcribes + reassembles itself.
- **Redis** — shared store (jobs/chunks/lineage) + the work queue. Scaled (Docker) setup.
- **Workers** — separate processes that pull chunks from Redis, transcribe, and (the last
  one) reassemble.
- **Model-server** — optional shared inference service holding the model once in RAM;
  API/workers call it over HTTP.

## The flow
- **Always:** API decodes + slices file into 20s chunks (2s overlap). The **map/split**.
- **Sync ("watch live"):** API transcribes chunks itself, streams the growing transcript
  over SSE as chunks finish. Connection-bound.
- **Async ("submit & poll"):** API drops chunks on Redis, returns a job ID. Workers pull
  and transcribe in parallel. Each job has a Redis counter of "remaining chunks";
  whichever worker **atomically** drives it to 0 reassembles the final transcript (the
  **reduce**). Browser polls the API, which reads Redis. Durable, survives disconnects.
- **Key structural win:** the transcribe-a-chunk code and the reassemble code are
  **shared** across sync/async, so the two modes can't diverge in output or lineage.

## Key design decisions (+ why + trade-off)
- **Chunk = unit of work.** Enables parallelism, partial results, cheap per-chunk retries,
  and fine-grained lineage. (Lineage is a *bonus* of chunking, not the reason for it.)
- **Sync vs async = two real things**, not fast/slow. Sync is connection-bound/live (SSE);
  async is durable/persisted (survives crashes, runs long, can batch overnight). Three
  axes bundled: delivery, timing, durability.
- **strong ⇒ remote.** Local stays tiny (protect the box, keep audio on-prem); "strong" =
  large-v3, hosted. A *deployment fact*, not a law — a GPU box could run strong on-prem,
  which is what a real intel deployment wants (audio never leaves the perimeter). (Free HF
  tier only serves large-v3 anyway.)
- **Admission control ≠ scaling.** `MAX_CONCURRENT_CHUNKS` caps in-flight chunks **per
  process** (a safety/rate limit). Adding workers is a **throughput lever**. Different
  problems — don't conflate them.
- **Backends behind one interface.** One method: audio + model → text. Local, model-server,
  and HF are interchangeable. Local uses a thread pool (CPU work); hosted uses coroutines
  (network wait).
- **Fallback is directional.** Fast tier degrades hosted→local tiny if HF fails (you still
  get output, recorded in lineage). Strong tier does **not** fall back — it fails clearly
  rather than silently give worse quality than asked. A failed chunk = a gap, not a crash;
  the rest of the file still transcribes.

## Concurrency (the star topic — know this cold)
- **asyncio for coordination** (uploads, queueing, SSE, polling, HTTP to HF — IO-bound).
- **Thread pool for local inference** (CPU-bound). Two reasons: (1) keep the blocking call
  *off* the event loop so the server stays responsive; (2) pipeline + priority-interleave
  so short/interactive chunks pass long batch ones.
- **Thread pool ≠ throughput.** Torch is **greedy** (one inference uses all cores) but
  **preemptible** (the OS time-slices a second one) → more threads add **zero** throughput
  on one box. Real throughput = **more processes**, not threads.
- **Why cap local concurrency, ranked:** (1) **CPU core oversubscription** — dominant;
  (2) **RAM occupancy** — per-inference *activations* aren't shared, so concurrency
  multiplies them → OOM wall for the *large* model (~nil for tiny; 4 concurrent tiny ≈
  1 GB); (3) cache thrash from switching — a footnote (~few %).
- **Weights vs activations:** weights load once and are shared (every model); activations
  are per-input, never shared. Attention makes activations bigger (O(seq²)) — it doesn't
  "save weights."
- `MAX_CONCURRENT_CHUNKS=4` is a **latency/fairness knob**, not throughput — an env var
  that should track core count + model.

## Load balancing (systems point)
- **Pull-queue (Redis)** self-balances by consumption — workers take work only when free;
  handles varying rates + backpressure. Better than round-robin for uneven, long work.
- **Round-robin (Service)** is right for **stateless, uniform, request/response** hops you
  *can't* queue: the HTTP API front door, and the model-server replicas if scaled.
- Worker→inference is kept **synchronous** because the worker owns the chunk's lineage +
  reassembly slot; getting the result back in-place keeps tracking trivial. A queue there
  would need correlation IDs + a return path for no gain.

## Observability & testing
- **Traced at the chunk level:** for each chunk — which worker, which backend, audio
  time-span, timings, retries, status, text. Structured **JSON logs** keyed by
  job_id/chunk_id/worker_id → drop straight into **Splunk**. `/healthz` for K8s probes
  (shallow — a prod readiness probe would also check Redis + model-server). Metrics (queue
  depth, chunks/sec) = next step.
- **Testing:** fast **mocked** unit tests for the logic (chunking, reassembly, auth,
  pipeline orchestration, Redis path via fakeredis) + **one real end-to-end** test asserted
  by **Word Error Rate** (ASR is never exact, so a fuzzy threshold is the correct
  criterion). Clever bit: the orchestration test's fake backend finishes chunks **in
  reverse order on purpose**, proving the reassembler restores order regardless of
  completion order.

## Known limitations / what I'd add (say these proactively — reads as senior)
1. **Sync doesn't scale with worker count** (SSE ties it to the API process). A known
   *limitation*, not a bug.
2. **Redis queue is FIFO — priority is lost in the distributed path** (priority only exists
   in the in-process scheduler).
3. **The one refactor that fixes both:** route sync through the workers too, on a
   **separate high-priority queue** (2 lanes: sync high, async low). Three wins: sync
   parallelism + priority restored + thinner API.
4. **Fat API** in local mode — should be thinned to receive/chunk/enqueue/serve.
5. **Failed chunk = silent gap** — production needs per-chunk timeouts/retries + a visible
   marker; also a job-level reaper so a stuck chunk doesn't hang the counter.
6. **Chunk audio parked in Redis as raw bytes** → object storage in prod.
7. **In-memory state by default** → Redis/durable DB for real durability.
8. **Static token auth** → OIDC/mTLS + K8s Secrets/Vault.
9. **Testing honesty:** async path verified with **fakeredis** + Docker-wired; a live
   multi-process real-Redis run likely wasn't executed on the Windows box. Claim
   "logically tested + Docker-wired," not "production-verified."

## Stack mapping (how it connects to their Spark/Airflow/K8s)
- **Spark** = distributed compute; my chunk→transcribe→reduce *is* map-reduce (my Redis
  queue is a hand-rolled shuffle).
- **Airflow** = orchestrator; a job is a DAG (split → N transcribe → reduce); lineage =
  task-instance history.
- **K8s** = stateless API + worker Deployments, Redis as a backing service, `/healthz`
  probes, scale via replicas; an HPA is the auto version of `--scale worker=N`.
- **Why not actually use them?** One box → Spark/Airflow would be overkill. Genuinely
  considered and rejected — the design *maps onto* them so adopting them later is a swap,
  not a rewrite. (Never used them in production; built the shapes by hand.)

## Positioning lines
- *"I built it in ~1 day under pressure. The ideas and architecture I stand behind; I
  tested end-to-end and read the logs. Some docs describe an earlier plan the code moved
  past, and there's a rough edge or two I'd polish. I'll happily walk any of it and tell
  you honestly what's solid vs. what I'd finish."*
- *"I'm the backend dev — I design code DevOps can scale: stateless workers, externalized
  state, health endpoints, env config. I know where my responsibility ends and theirs
  begins."*
