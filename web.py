"""
Investor Conviction Ladder — web app.

Upload a pitch deck in the browser, the server calls the Claude API, and the
one-page PDF report comes back as a download.

Run locally:
    uvicorn web:app --reload

Deploy (Railway): the Procfile runs uvicorn against $PORT.

Environment
-----------
ANTHROPIC_API_KEY   required — your key from console.anthropic.com
APP_PASSWORD        strongly recommended — gates the app behind HTTP Basic
                    auth. Without it, anyone with the URL can spend your
                    API budget.
APP_USERNAME        optional, defaults to "ten" (only used with APP_PASSWORD)
MAX_CONCURRENT      optional, default 2 — simultaneous analyses
JOB_TTL_SECONDS     optional, default 3600 — how long a finished report stays
                    downloadable in memory
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import date

import anthropic
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import engine
from report_pdf import render_report_bytes

APP_USERNAME = os.getenv("APP_USERNAME", "ten")
APP_PASSWORD = os.getenv("APP_PASSWORD")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "3600"))

app = FastAPI(title="Investor Conviction Ladder", docs_url=None, redoc_url=None)
_basic = HTTPBasic(auto_error=False)
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


# --- Auth ---------------------------------------------------------------------


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """HTTP Basic gate. A no-op when APP_PASSWORD is unset."""
    if not APP_PASSWORD:
        return
    ok = credentials is not None and (
        secrets.compare_digest(credentials.username, APP_USERNAME)
        and secrets.compare_digest(credentials.password, APP_PASSWORD)
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authorized",
            headers={"WWW-Authenticate": "Basic"},
        )


# --- Job store ----------------------------------------------------------------


@dataclass
class Job:
    id: str
    filename: str
    status: str = "queued"        # queued | running | done | error
    note: str | None = None
    error: str | None = None
    ticks: int = 0                 # streamed-chunk heartbeat
    started: float = field(default_factory=time.time)
    finished: float | None = None
    analysis: dict | None = None
    pdf: bytes | None = None

    def public(self) -> dict:
        elapsed = (self.finished or time.time()) - self.started
        data = {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "note": self.note,
            "error": self.error,
            "ticks": self.ticks,
            "elapsed": round(elapsed, 1),
        }
        if self.status == "done" and self.analysis:
            data["company_name"] = self.analysis["company_name"]
            data["overall_score"] = self.analysis["overall_score"]
            data["verdict"] = self.analysis["verdict"]
            data["diagnosis"] = self.analysis["diagnosis"]
            data["stages"] = [
                {k: s[k] for k in ("number", "name", "score", "status")}
                for s in self.analysis["stages"]
            ]
        return data


JOBS: dict[str, Job] = {}

# asyncio only holds a weak reference to a running task, so a task with no
# other referent can be garbage-collected mid-flight. Keep a strong reference
# until it finishes.
_TASKS: set[asyncio.Task] = set()


def sweep_jobs() -> None:
    """Drop finished jobs past their TTL so memory doesn't grow unbounded."""
    cutoff = time.time() - JOB_TTL_SECONDS
    for job_id in [
        j.id for j in JOBS.values()
        if j.finished is not None and j.finished < cutoff
    ]:
        JOBS.pop(job_id, None)


def safe_stem(filename: str) -> str:
    stem = re.sub(r"\.pdf$", "", filename or "deck", flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9 _.-]+", "", stem).strip() or "deck"
    return stem[:80]


# --- Analysis task ------------------------------------------------------------


async def run_job(job: Job, data: bytes, effort: str) -> None:
    async with _semaphore:
        job.status = "running"
        job.started = time.time()
        loop = asyncio.get_running_loop()

        def bump() -> None:
            job.ticks += 1

        def note(message: str) -> None:
            job.note = message

        try:
            analysis = await loop.run_in_executor(
                None,
                lambda: engine.analyze_pdf_bytes(
                    data, effort=effort, on_progress=bump, on_note=note
                ),
            )
            report_date = date.today().strftime("%B %d, %Y")
            job.pdf = await loop.run_in_executor(
                None, lambda: render_report_bytes(analysis, report_date)
            )
            job.analysis = analysis
            job.status = "done"
        except engine.AnalysisError as e:
            job.status, job.error = "error", str(e)
        except anthropic.AuthenticationError:
            job.status = "error"
            job.error = (
                "The server's ANTHROPIC_API_KEY is missing or invalid. Set it "
                "in the Railway service variables and redeploy."
            )
        except anthropic.RateLimitError:
            job.status = "error"
            job.error = "Anthropic rate limit hit. Wait a moment and retry."
        except anthropic.APIError as e:
            job.status, job.error = "error", f"Anthropic API error: {e}"
        except ValueError as e:  # normalize() rejects an incomplete analysis
            job.status, job.error = "error", str(e)
        except Exception as e:  # noqa: BLE001 - surface, don't swallow
            job.status = "error"
            job.error = f"Unexpected error: {type(e).__name__}: {e}"
        finally:
            job.finished = time.time()
            sweep_jobs()


# --- Routes -------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "model": engine.MODEL,
        "api_key_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "auth_enabled": bool(APP_PASSWORD),
        "active_jobs": sum(1 for j in JOBS.values() if j.status in
                           ("queued", "running")),
    }


@app.get("/", response_class=HTMLResponse)
async def index(_: None = Depends(require_auth)) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


@app.post("/api/analyze")
async def start_analysis(
    _: None = Depends(require_auth),
    deck: UploadFile = File(...),
    effort: str = Form("high"),
) -> JSONResponse:
    if effort not in engine.EFFORT_LEVELS:
        raise HTTPException(400, f"effort must be one of {engine.EFFORT_LEVELS}")

    data = await deck.read()
    try:
        engine.validate_pdf(data, deck.filename or "deck.pdf")
    except engine.AnalysisError as e:
        raise HTTPException(400, str(e)) from e

    sweep_jobs()
    job = Job(id=uuid.uuid4().hex[:12], filename=safe_stem(deck.filename or "deck"))
    JOBS[job.id] = job
    task = asyncio.create_task(run_job(job, data, effort))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return JSONResponse({"id": job.id}, status_code=202)


def _get_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found or expired.")
    return job


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str, _: None = Depends(require_auth)) -> dict:
    return _get_job(job_id).public()


@app.get("/api/jobs/{job_id}/report")
async def job_report(job_id: str, _: None = Depends(require_auth)) -> Response:
    job = _get_job(job_id)
    if job.status != "done" or job.pdf is None:
        raise HTTPException(409, "Report is not ready.")
    name = f"{job.filename} - Conviction Ladder.pdf"
    return Response(
        content=job.pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/jobs/{job_id}/analysis")
async def job_analysis(job_id: str, _: None = Depends(require_auth)) -> Response:
    job = _get_job(job_id)
    if job.status != "done" or job.analysis is None:
        raise HTTPException(409, "Analysis is not ready.")
    return Response(
        content=json.dumps(job.analysis, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition":
                f'attachment; filename="{job.filename} - analysis.json"'
        },
    )


# --- Frontend -----------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Investor Conviction Ladder</title>
<style>
  :root {
    --navy:#16283A; --blue:#2C5F8D; --panel:#EEF2F6; --grid:#C3CDD8;
    --ink:#1A1A1A; --muted:#6B7683; --green:#1E7A3C; --amber:#E8833A;
    --red:#B3261E; --bg:#F7F9FB;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }
  header {
    background:var(--navy); color:#fff; padding:18px 24px;
    display:flex; align-items:baseline; gap:14px; flex-wrap:wrap;
  }
  header h1 { margin:0; font-size:17px; letter-spacing:.05em; }
  header span { color:#9DB3C7; font-size:12.5px; }
  main { max-width:940px; margin:0 auto; padding:28px 24px 64px; }
  .card {
    background:#fff; border:1px solid var(--grid); border-radius:8px;
    padding:22px; margin-bottom:20px;
  }
  #drop {
    border:2px dashed var(--grid); border-radius:8px; padding:38px 20px;
    text-align:center; cursor:pointer; transition:.15s; background:var(--panel);
  }
  #drop:hover, #drop.over { border-color:var(--blue); background:#E4EDF5; }
  #drop strong { color:var(--blue); }
  #drop small { display:block; color:var(--muted); margin-top:8px; }
  .row { display:flex; gap:14px; align-items:center; flex-wrap:wrap; margin-top:18px; }
  label { font-size:13px; color:var(--muted); }
  select {
    padding:7px 10px; border:1px solid var(--grid); border-radius:5px;
    background:#fff; font:inherit; font-size:14px;
  }
  button {
    background:var(--blue); color:#fff; border:0; border-radius:5px;
    padding:10px 20px; font:inherit; font-weight:600; cursor:pointer;
  }
  button:disabled { background:var(--grid); cursor:not-allowed; }
  button.ghost { background:#fff; color:var(--blue); border:1px solid var(--blue); }
  .hidden { display:none !important; }
  .bar { height:5px; background:var(--panel); border-radius:3px; overflow:hidden; }
  .bar i {
    display:block; height:100%; width:35%; background:var(--blue);
    animation:slide 1.4s ease-in-out infinite;
  }
  @keyframes slide { 0%{margin-left:-35%} 100%{margin-left:100%} }
  .err {
    background:#FCEBEA; border:1px solid #E8B4B0; color:#8E1F19;
    padding:12px 14px; border-radius:6px; font-size:14px;
  }
  .verdict { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
  .verdict b { font-size:30px; }
  .pill { padding:4px 12px; border-radius:20px; color:#fff; font-weight:600; font-size:13px; }
  table { width:100%; border-collapse:collapse; margin-top:16px; font-size:13.5px; }
  th { background:var(--blue); color:#fff; text-align:left; padding:7px 9px; font-size:12px; }
  td { padding:7px 9px; border-bottom:1px solid var(--grid); }
  td.n { color:var(--muted); width:30px; }
  td.s { text-align:center; font-weight:600; width:56px; }
  td.st { text-align:center; width:110px; }
  dl { margin:18px 0 0; display:grid; grid-template-columns:170px 1fr; gap:9px 14px; font-size:13.5px; }
  dt { font-weight:600; color:var(--blue); }
  dd { margin:0; }
  .muted { color:var(--muted); font-size:13px; }
</style>
</head>
<body>
<header>
  <h1>INVESTOR CONVICTION LADDER</h1>
  <span>TEN Capital Network</span>
</header>
<main>

  <div class="card" id="upload-card">
    <div id="drop">
      <strong>Drop a pitch deck PDF here</strong> or click to choose a file
      <small>Max 20MB. The deck is sent to the Claude API for scoring and is not stored.</small>
      <div id="chosen" class="muted" style="margin-top:12px;"></div>
    </div>
    <input type="file" id="file" accept="application/pdf,.pdf" class="hidden">
    <div class="row">
      <label for="effort">Effort</label>
      <select id="effort">
        <option value="low">low — fastest</option>
        <option value="medium">medium</option>
        <option value="high" selected>high — default</option>
        <option value="xhigh">xhigh</option>
        <option value="max">max — deepest, slowest</option>
      </select>
      <button id="go" disabled>Run conviction analysis</button>
    </div>
  </div>

  <div class="card hidden" id="progress-card">
    <div style="display:flex;justify-content:space-between;align-items:baseline;">
      <strong id="prog-title">Scoring 10 conviction stages…</strong>
      <span class="muted" id="prog-meta"></span>
    </div>
    <div class="bar" style="margin-top:12px;"><i></i></div>
    <p class="muted" id="prog-note" style="margin-bottom:0;">
      Opus 5 reads the whole deck before scoring. This usually takes one to
      three minutes — leave the tab open.
    </p>
  </div>

  <div class="card hidden" id="error-card"><div class="err" id="error-text"></div></div>

  <div class="card hidden" id="result-card">
    <div class="verdict">
      <b id="score"></b>
      <span class="muted">/ 50</span>
      <span class="pill" id="verdict"></span>
      <span style="flex:1"></span>
      <span id="company" style="font-weight:600;"></span>
    </div>
    <table>
      <thead><tr><th>#</th><th>Stage</th><th style="text-align:center">Score</th>
        <th style="text-align:center">Status</th></tr></thead>
      <tbody id="stages"></tbody>
    </table>
    <dl id="diagnosis"></dl>
    <div class="row">
      <button id="dl">Download PDF report</button>
      <button id="dl-json" class="ghost">Download JSON</button>
      <button id="again" class="ghost">Analyze another deck</button>
    </div>
  </div>

</main>
<script>
const $ = (id) => document.getElementById(id);
const drop = $("drop"), fileInput = $("file");
let chosenFile = null, jobId = null, poller = null;

drop.addEventListener("click", () => fileInput.click());
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault(); drop.classList.remove("over");
  if (e.dataTransfer.files.length) pick(e.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) pick(fileInput.files[0]);
});

function pick(f) {
  if (!/\\.pdf$/i.test(f.name)) { showError("Please choose a PDF file."); return; }
  chosenFile = f;
  $("chosen").textContent = `${f.name} — ${(f.size / 1048576).toFixed(1)}MB`;
  $("go").disabled = false;
  hide("error-card");
}

function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }
function showError(msg) { $("error-text").textContent = msg; show("error-card"); }

const VERDICT_COLOR = {
  "Strong Conviction": "#1E7A3C", "Advance with Conditions": "#2C5F8D",
  "Borderline": "#E8833A", "Pass": "#B3261E",
};
const SCORE_COLOR = { 5:"#1E7A3C", 4:"#1E7A3C", 3:"#2C5F8D", 2:"#D2691E", 1:"#B3261E" };

$("go").addEventListener("click", async () => {
  if (!chosenFile) return;
  $("go").disabled = true;
  hide("error-card"); hide("result-card"); hide("upload-card"); show("progress-card");

  const body = new FormData();
  body.append("deck", chosenFile);
  body.append("effort", $("effort").value);

  try {
    const res = await fetch("/api/analyze", { method: "POST", body });
    if (!res.ok) throw new Error((await res.json()).detail || `HTTP ${res.status}`);
    jobId = (await res.json()).id;
    poller = setInterval(poll, 2000);
    poll();
  } catch (e) {
    hide("progress-card"); show("upload-card"); $("go").disabled = false;
    showError(e.message);
  }
});

async function poll() {
  try {
    const res = await fetch(`/api/jobs/${jobId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const job = await res.json();
    $("prog-meta").textContent = `${Math.round(job.elapsed)}s elapsed`;
    if (job.note) $("prog-note").textContent = job.note;
    if (job.status === "done") { clearInterval(poller); render(job); }
    else if (job.status === "error") {
      clearInterval(poller);
      hide("progress-card"); show("upload-card"); $("go").disabled = false;
      showError(job.error);
    }
  } catch (e) {
    clearInterval(poller);
    hide("progress-card"); show("upload-card"); $("go").disabled = false;
    showError("Lost contact with the server: " + e.message);
  }
}

const DIAG_LABELS = [
  ["conviction_breaks_at", "Conviction breaks at"],
  ["partner_meeting", "Partner meeting?"],
  ["biggest_red_flag", "Biggest red flag"],
  ["highest_leverage", "Highest leverage"],
  ["increase_p_invest", "To increase P(invest)"],
];

function render(job) {
  hide("progress-card"); show("result-card");
  $("score").textContent = job.overall_score;
  $("company").textContent = job.company_name;
  const v = $("verdict");
  v.textContent = job.verdict;
  v.style.background = VERDICT_COLOR[job.verdict] || "#2C5F8D";

  $("stages").innerHTML = job.stages.map((s) => `
    <tr><td class="n">${s.number}</td><td>${esc(s.name)}</td>
    <td class="s" style="color:${SCORE_COLOR[s.score]}">${s.score}/5</td>
    <td class="st" style="color:${SCORE_COLOR[s.score]}">${esc(s.status)}</td></tr>`
  ).join("");

  $("diagnosis").innerHTML = DIAG_LABELS.map(
    ([k, label]) => `<dt>${label}</dt><dd>${esc(job.diagnosis[k] || "—")}</dd>`
  ).join("");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

$("dl").addEventListener("click", () => location.assign(`/api/jobs/${jobId}/report`));
$("dl-json").addEventListener("click", () => location.assign(`/api/jobs/${jobId}/analysis`));
$("again").addEventListener("click", () => {
  chosenFile = null; jobId = null;
  fileInput.value = ""; $("chosen").textContent = ""; $("go").disabled = true;
  hide("result-card"); show("upload-card");
});
</script>
</body>
</html>
"""
