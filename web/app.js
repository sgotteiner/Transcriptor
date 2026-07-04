"use strict";

const $ = (id) => document.getElementById(id);
const fileInput = $("file");
const dropzone = $("dropzone");
const fileName = $("fileName");
const fileMeta = $("fileMeta");
const tokenInput = $("token");
const goBtn = $("go");
const spinner = $("spinner");
const btnLabel = $("btnLabel");
const statusEl = $("status");
const results = $("results");
const resultsEmpty = $("resultsEmpty");
const cardTpl = $("cardTpl");
const localRadio = $("be-local");
const matrixNote = $("matrixNote");

const radio = (name) => document.querySelector(`input[name="${name}"]:checked`).value;

const PILL = {
  done: "bg-emerald-400/15 text-emerald-400",
  processing: "bg-accent/15 text-accent",
  pending: "bg-slate-400/15 text-muted",
  failed: "bg-red-400/15 text-red-400",
};

// --- File selection (multiple; click + drag & drop) -----------------------
function showFiles(files) {
  const n = files.length;
  if (!n) return;
  fileName.textContent = n === 1 ? files[0].name : `${n} files selected`;
  const mb = ([...files].reduce((s, f) => s + f.size, 0) / 1024 / 1024).toFixed(1);
  fileMeta.textContent = `${mb} MB total · decoded locally`;
  dropzone.classList.add("border-solid");
  dropzone.classList.remove("border-dashed");
}
fileInput.addEventListener("change", () => showFiles(fileInput.files));

const DRAG = ["border-accent", "bg-[rgba(124,139,255,0.08)]"];
["dragenter", "dragover"].forEach((e) =>
  dropzone.addEventListener(e, (ev) => { ev.preventDefault(); dropzone.classList.add(...DRAG); }));
["dragleave", "drop"].forEach((e) =>
  dropzone.addEventListener(e, (ev) => { ev.preventDefault(); dropzone.classList.remove(...DRAG); }));
dropzone.addEventListener("drop", (ev) => {
  if (ev.dataTransfer.files.length) { fileInput.files = ev.dataTransfer.files; showFiles(fileInput.files); }
});

// --- Model↔backend matrix: strong ⇒ remote; local ⇒ fast only -------------
function applyMatrix() {
  const strong = radio("tier") === "strong";
  if (strong) {
    $("be-hosted").checked = true;
    localRadio.disabled = true;
    matrixNote.textContent = "Strong model is remote-only — backend forced to Remote.";
  } else {
    localRadio.disabled = false;
    matrixNote.textContent = radio("backend") === "hosted"
      ? "Remote · fast offloads compute to Hugging Face."
      : "Local · fast keeps audio on your machine (no token needed).";
  }
}
document.querySelectorAll('input[name="tier"], input[name="backend"]')
  .forEach((el) => el.addEventListener("change", applyMatrix));
applyMatrix();

// --- Helpers ---------------------------------------------------------------
function headers() {
  const h = {};
  const tok = tokenInput.value.trim();
  if (tok) h["X-API-Key"] = tok;
  return h;
}

function setBusy(busy) {
  goBtn.disabled = busy;
  spinner.classList.toggle("hidden", !busy);
  btnLabel.classList.toggle("pl-5", busy);
}

// --- Per-file result card --------------------------------------------------
function makeCard(filename) {
  if (resultsEmpty && resultsEmpty.parentNode) resultsEmpty.remove();
  const node = cardTpl.content.firstElementChild.cloneNode(true);
  results.appendChild(node);
  const q = (sel) => node.querySelector(sel);
  const card = {
    title: q("[data-title]"), jobid: q("[data-jobid]"), status: q("[data-status]"),
    pct: q("[data-pct]"), bar: q("[data-bar]"), transcript: q("[data-transcript]"),
    lineage: q("[data-lineage]"), count: q("[data-count]"),
  };
  card.title.textContent = filename;
  return card;
}

function cardTranscript(c, text, streaming) {
  c.transcript.textContent = "";
  if (!text && !streaming) {
    const s = document.createElement("span");
    s.className = "text-faint";
    s.textContent = "Waiting…";
    c.transcript.appendChild(s);
    return;
  }
  c.transcript.textContent = text;
  if (streaming) {
    const caret = document.createElement("span");
    caret.className = "inline-block w-2 h-[1.05em] ml-0.5 align-text-bottom bg-accent rounded-[1px] animate-blink";
    c.transcript.appendChild(caret);
  }
}

function cardProgress(c, done, total) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  c.bar.style.width = pct + "%";
  c.pct.textContent = pct + "%";
  c.status.textContent = total ? `${done}/${total} chunks` : "";
}

function renderLineage(c, chunks) {
  if (!chunks.length) return;
  c.count.textContent = `(${chunks.length})`;
  c.lineage.innerHTML = "";
  const td = "px-2.5 py-2 border-b border-edge";
  const mono = `${td} font-mono text-[11px] text-muted`;
  for (const k of chunks) {
    const span = `${(k.start_ms / 1000).toFixed(1)}–${(k.end_ms / 1000).toFixed(1)}s`;
    const tr = document.createElement("tr");
    tr.className = "hover:bg-white/[0.02]";
    tr.innerHTML = `
      <td class="${td}">${k.index}</td>
      <td class="${mono}">${k.chunk_id}</td>
      <td class="${mono}">${span}</td>
      <td class="${td}"><span class="inline-block px-2 py-[2px] rounded-full text-[10.5px] font-semibold ${PILL[k.status] || ""}">${k.status}</span></td>
      <td class="${td}">${k.backend_used || "—"}</td>
      <td class="${mono}">${k.worker_id || "—"}</td>
      <td class="${mono}">${k.duration_ms ?? "—"}</td>`;
    c.lineage.appendChild(tr);
  }
}

async function loadLineage(c, jobId) {
  try {
    const r = await fetch(`/jobs/${jobId}/chunks`, { headers: headers() });
    if (r.ok) {
      const chunks = (await r.json()).chunks;
      renderLineage(c, chunks);
      return chunks;
    }
  } catch (_) { /* best-effort */ }
  return null;
}

function parseSSE(buffer, onEvent) {
  const parts = buffer.split(/\r?\n\r?\n/);
  const tail = parts.pop();
  for (const block of parts) {
    const data = block.split(/\r?\n/)
      .filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).trim())
      .join("\n");
    if (data) onEvent(JSON.parse(data));
  }
  return tail;
}

async function runSyncCard(c, resp) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let jobId = null;
  let timer = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = parseSSE(buffer, (evt) => {
      if (evt.job_id && !jobId) {
        jobId = evt.job_id;
        c.jobid.textContent = jobId;
        timer = setInterval(() => loadLineage(c, jobId), 1000); // live lineage
      }
      if (evt.transcript !== undefined) cardTranscript(c, evt.transcript, evt.type !== "done");
      if (evt.total !== undefined) cardProgress(c, evt.completed, evt.total);
      if (evt.type === "done") c.status.textContent = `done · ${evt.status}`;
    });
  }
  if (timer) clearInterval(timer);
  if (jobId) await loadLineage(c, jobId);
}

async function pollAsyncCard(c, jobId) {
  c.jobid.textContent = jobId;
  while (true) {
    const jr = await fetch(`/jobs/${jobId}`, { headers: headers() });
    if (!jr.ok) { c.status.textContent = "error"; return; }
    const job = await jr.json();
    const running = job.status !== "done" && job.status !== "failed";
    cardTranscript(c, job.transcript || "", running);
    const chunks = await loadLineage(c, jobId);
    const done = chunks ? chunks.filter((k) => k.status === "done" || k.status === "failed").length : 0;
    cardProgress(c, done, job.chunk_count);
    if (!running) { cardProgress(c, job.chunk_count, job.chunk_count); c.status.textContent = `done · ${job.status}`; return; }
    await new Promise((res) => setTimeout(res, 600));
  }
}

async function transcribeFile(file) {
  const c = makeCard(file.name);
  cardTranscript(c, "", true);
  const fd = new FormData();
  fd.append("file", file);
  fd.append("tier", radio("tier"));
  fd.append("backend", radio("backend"));
  fd.append("mode", radio("mode"));
  try {
    const resp = await fetch("/jobs", { method: "POST", body: fd, headers: headers() });
    if (!resp.ok) {
      c.status.textContent = `error · ${resp.status}`;
      cardTranscript(c, await resp.text().catch(() => "request failed"), false);
      return;
    }
    if (radio("mode") === "sync") {
      await runSyncCard(c, resp);
    } else {
      const { job_id } = await resp.json();
      await pollAsyncCard(c, job_id);
    }
  } catch (err) {
    c.status.textContent = "error";
    cardTranscript(c, String(err), false);
  }
}

// --- Submit (one job per file, concurrently) ------------------------------
goBtn.addEventListener("click", async () => {
  const files = [...fileInput.files];
  if (!files.length) { statusEl.textContent = "pick a file first"; return; }
  setBusy(true);
  statusEl.textContent = `transcribing ${files.length} file${files.length > 1 ? "s" : ""}…`;
  try {
    await Promise.all(files.map(transcribeFile));
    statusEl.textContent = `done · ${files.length} file${files.length > 1 ? "s" : ""}`;
  } finally {
    setBusy(false);
  }
});
