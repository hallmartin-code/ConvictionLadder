"""
Investor Conviction Ladder — web app.

Upload a pitch deck (PDF or PowerPoint) in the browser, the server calls the Claude API, and the
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
RESEND_API_KEY      optional — enables emailing each finished report
REPORT_EMAIL_TO     recipients, default Info@tencapital.group
REPORT_EMAIL_FROM   sender; must be on a Resend-verified domain
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import anthropic
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import engine
import mailer
from report_pdf import render_report_bytes

APP_USERNAME = os.getenv("APP_USERNAME", "ten")
APP_PASSWORD = os.getenv("APP_PASSWORD")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "3600"))

app = FastAPI(title="Investor Conviction Ladder", docs_url=None, redoc_url=None)

# Brand assets. Deliberately not behind require_auth: the browser fetches the
# favicon before the user has authenticated, and a 401 there just shows a blank
# tab icon on the login prompt. Nothing in here is sensitive.
STATIC_DIR = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
_basic = HTTPBasic(auto_error=False)
_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# Say it at boot, in the deploy log, rather than letting every upload discover
# it one at a time. Uvicorn's stderr is what Railway shows.
if not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
    print(
        f"WARNING: {engine.no_credentials_message()}",
        file=sys.stderr,
        flush=True,
    )


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
    source: str = ""            # uploaded name, extension included
    status: str = "queued"        # queued | running | done | error
    note: str | None = None
    error: str | None = None
    ticks: int = 0                 # streamed-chunk heartbeat
    started: float = field(default_factory=time.time)
    finished: float | None = None
    analysis: dict | None = None
    pdf: bytes | None = None
    email: str | None = None    # delivery status, surfaced in the UI

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
            "email": self.email,
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
    stem = re.sub(r"\.(pdf|pptx|ppt)$", "", filename or "deck", flags=re.I)
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
                lambda: engine.analyze_deck_bytes(
                    data, effort=effort, on_progress=bump, on_note=note
                ),
            )
            report_date = date.today().strftime("%B %d, %Y")
            job.pdf = await loop.run_in_executor(
                None, lambda: render_report_bytes(analysis, report_date)
            )
            job.analysis = analysis
            job.status = "done"

            # The report is finished and downloadable from here on. Emailing is
            # a side errand: run it after the status flips, and let a failure
            # show up as a note rather than costing the user the analysis.
            if mailer.is_configured():
                try:
                    job.email = await loop.run_in_executor(
                        None,
                        lambda: mailer.send_report(
                            analysis, job.pdf, report_date,
                            source_filename=job.source or job.filename,
                        ),
                    )
                except mailer.MailError as e:
                    job.email = f"Not emailed — {e}"
                    print(f"[email] job {job.id}: {e}", file=sys.stderr, flush=True)
            else:
                job.email = None
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


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "model": engine.MODEL,
        "api_key_configured": bool((os.getenv("ANTHROPIC_API_KEY") or "").strip()),
        "auth_enabled": bool(APP_PASSWORD),
        "email_configured": mailer.is_configured(),
        "email_to": mailer.recipients() if mailer.is_configured() else [],
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
        engine.validate_deck(data, deck.filename or "deck")
    except engine.AnalysisError as e:
        raise HTTPException(400, str(e)) from e

    sweep_jobs()
    job = Job(
        id=uuid.uuid4().hex[:12],
        filename=safe_stem(deck.filename or "deck"),
        source=deck.filename or "",
    )
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

# The page lives in index.html so the design can be edited without touching
# Python. Read once at import: a missing or broken template should fail the
# deploy loudly, not every request quietly.
TEMPLATE_PATH = Path(__file__).with_name("index.html")

try:
    INDEX_HTML = (
        TEMPLATE_PATH.read_text(encoding="utf-8")
        .replace("{{MAX_BYTES}}", str(engine.MAX_PDF_BYTES))
        .replace("{{MAX_MB}}", str(engine.MAX_PDF_BYTES // (1024 * 1024)))
    )
except FileNotFoundError:  # pragma: no cover - packaging mistake
    raise RuntimeError(
        f"index.html is missing (expected at {TEMPLATE_PATH}). It ships with "
        "the app and must be committed alongside web.py."
    ) from None
