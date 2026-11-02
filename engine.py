"""
Conviction Ladder analysis engine.

Shared by the CLI (`analyze.py`) and the web app (`web.py`): takes pitch deck
bytes (PDF, or PowerPoint converted to PDF), calls the Claude API with a
structured-output schema, and returns a normalized analysis dict ready for
`report_pdf.build_report`.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

import anthropic

import conviction_ladder as cl

# Load .env for local dev if python-dotenv is installed. Point at the file next
# to this module rather than letting dotenv walk up from the current directory:
# `uvicorn web:app` or `python ...\analyze.py` started from anywhere but the
# project root would otherwise find nothing and the SDK would fail to resolve
# credentials. A missing path is a no-op, which is the Railway case — there the
# real environment already holds the variables.
ENV_PATH = Path(__file__).with_name(".env")

try:
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH)
except ModuleNotFoundError:  # pragma: no cover - optional convenience
    pass

MODEL = "claude-opus-5"
MAX_TOKENS = 32000  # streamed, so no HTTP-timeout concern

# Opus 5's safety classifiers can decline a request. `fallbacks` re-serves a
# declined request on the fallback model inside the same call, so a false
# positive on an otherwise benign deck doesn't lose the run. Costs nothing when
# nothing refuses. This is the array form, which pairs with the -06-01 header;
# newer SDKs also accept fallbacks="default" with the -07-01 header.
FALLBACK_BETA = "server-side-fallback-2026-06-01"
FALLBACK_MODELS = [{"model": "claude-opus-4-8"}]

# Anthropic caps a request at 32MB. Base64 inflates by ~4/3, so the raw PDF
# has to stay comfortably under that.
MAX_PDF_BYTES = 20 * 1024 * 1024

EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]

# The API reads PDFs, not PowerPoint, so .pptx/.ppt decks are rendered to PDF
# with headless LibreOffice first. That keeps charts and layout visible to the
# model, which a text extraction would lose. Railway gets LibreOffice from
# nixpacks.toml; SOFFICE_PATH overrides discovery.
CONVERT_TIMEOUT_SECONDS = 180
_WINDOWS_SOFFICE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")


class AnalysisError(RuntimeError):
    """Raised for problems the caller should surface to a user verbatim."""


def is_deployed() -> bool:
    """True when running on the hosting platform rather than a dev machine.

    Railway injects RAILWAY_* into every service. The `.env` check is the
    backstop: it is gitignored, so it is never part of the deployed image.
    """
    return any(k.startswith("RAILWAY_") for k in os.environ) or not ENV_PATH.exists()


def no_credentials_message() -> str:
    """The fix for a missing key, phrased for wherever this is running.

    On the server, telling someone to export a shell variable is useless — the
    process is already up and the only lever is the platform's variables tab.
    """
    if is_deployed():
        return (
            "The server has no Anthropic API key configured. Add "
            "ANTHROPIC_API_KEY in the Railway service's Variables tab and "
            "redeploy. The project's .env is gitignored, so it is never part "
            "of the deployed image — the variable has to be set on the service "
            "itself. Get a key at https://console.anthropic.com/settings/keys"
        )
    return (
        "No Anthropic credentials found.\n"
        f"Add ANTHROPIC_API_KEY to {ENV_PATH} (copy .env.example), or set it "
        "in the environment:\n"
        '    $env:ANTHROPIC_API_KEY = "sk-ant-..."\n'
        "Get a key at https://console.anthropic.com/settings/keys"
    )


def build_client(api_key: str | None = None) -> anthropic.Anthropic:
    """Construct the SDK client, or fail with a message a user can act on.

    With nothing to authenticate with, the SDK raises a bare TypeError
    ("Could not resolve authentication method...") at construction time. Catch
    the empty case here so both front ends report the fix instead of a
    traceback. An env var set to an empty or whitespace-only string is the same
    situation as an unset one, but the SDK would carry it as far as a 401.
    """
    key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY")
    if key is not None:
        key = key.strip()

    if not key and not (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip():
        raise AnalysisError(no_credentials_message())

    try:
        return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    except TypeError as e:  # SDK's unresolved-credentials guard
        if "authentication method" not in str(e):
            raise
        raise AnalysisError(no_credentials_message()) from e


def deck_kind(data: bytes) -> str | None:
    """Identify a deck by its bytes: "pdf", "pptx", "ppt", or None.

    Sniffed rather than trusted from the filename or browser MIME type, which
    are both whatever the client says they are.
    """
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "ppt/presentation.xml" in z.namelist():
                    return "pptx"
        except zipfile.BadZipFile:
            return None
        return None
    if data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):  # OLE2: legacy .ppt
        return "ppt"
    return None


def validate_deck(data: bytes, filename: str = "deck.pdf") -> str:
    """Reject unsupported or oversized uploads before spending a call.

    Returns the deck kind from `deck_kind`.
    """
    if not data:
        raise AnalysisError("The uploaded file is empty.")
    kind = deck_kind(data)
    if kind is None:
        raise AnalysisError(
            f"{filename} is not a PDF or PowerPoint file. Upload a .pdf or "
            ".pptx and try again."
        )
    if len(data) > MAX_PDF_BYTES:
        mb = len(data) / (1024 * 1024)
        raise AnalysisError(
            f"{filename} is {mb:.1f}MB. The limit is "
            f"{MAX_PDF_BYTES // (1024 * 1024)}MB — re-export the deck at lower "
            "image quality and try again."
        )
    return kind


def find_soffice() -> str | None:
    override = (os.getenv("SOFFICE_PATH") or "").strip()
    if override:
        return override
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return str(_WINDOWS_SOFFICE) if _WINDOWS_SOFFICE.exists() else None


def convert_to_pdf(data: bytes, kind: str) -> bytes:
    """Render a PowerPoint deck to PDF bytes with headless LibreOffice."""
    soffice = find_soffice()
    if soffice is None:
        raise AnalysisError(
            "PowerPoint conversion is unavailable: LibreOffice is not "
            "installed on this server. Export the deck to PDF and upload that "
            "instead."
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / f"deck.{kind}"
        src.write_bytes(data)
        # A private profile per call: concurrent conversions sharing the
        # default profile lock each other out and fail silently.
        profile = (tmp_path / "profile").as_uri()
        try:
            subprocess.run(
                [
                    soffice, f"-env:UserInstallation={profile}",
                    "--headless", "--norestore",
                    "--convert-to", "pdf", "--outdir", str(tmp_path), str(src),
                ],
                capture_output=True,
                timeout=CONVERT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise AnalysisError(
                "Converting the PowerPoint deck to PDF timed out. Export it to "
                "PDF and upload that instead."
            ) from e

        out = tmp_path / "deck.pdf"
        if not out.exists() or not out.read_bytes().startswith(b"%PDF"):
            raise AnalysisError(
                "The PowerPoint deck could not be converted to PDF. It may be "
                "corrupt or password-protected — export it to PDF and upload "
                "that instead."
            )
        pdf = out.read_bytes()

    if len(pdf) > MAX_PDF_BYTES:
        raise AnalysisError(
            f"The deck converts to a {len(pdf) / (1024 * 1024):.1f}MB PDF, over "
            f"the {MAX_PDF_BYTES // (1024 * 1024)}MB limit. Compress its images "
            "and try again."
        )
    return pdf


def _request_kwargs(pdf_b64: str, effort: str) -> dict:
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": cl.SYSTEM_PROMPT,
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": cl.ANALYSIS_SCHEMA},
        },
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": cl.build_prompt()},
                ],
            }
        ],
    }


def _consume(stream_ctx, on_progress: Callable[[], None] | None):
    with stream_ctx as stream:
        for _ in stream.text_stream:
            if on_progress:
                on_progress()
        return stream.get_final_message()


def call_model(
    client: anthropic.Anthropic,
    pdf_b64: str,
    effort: str = "high",
    on_progress: Callable[[], None] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> dict:
    """Run the analysis and return the raw (un-normalized) JSON dict."""
    kwargs = _request_kwargs(pdf_b64, effort)

    try:
        message = _consume(
            client.beta.messages.stream(
                betas=[FALLBACK_BETA], fallbacks=FALLBACK_MODELS, **kwargs
            ),
            on_progress,
        )
    except anthropic.BadRequestError as e:
        # The refusal-fallback beta is optional. If this account or SDK build
        # doesn't accept it, run the same request without it rather than fail.
        if "fallback" not in str(e).lower():
            raise
        if on_note:
            on_note("Refusal fallbacks unavailable; retrying without them.")
        message = _consume(client.messages.stream(**kwargs), on_progress)

    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) or "unspecified"
        raise AnalysisError(
            f"The model declined this request (category: {category}). "
            "No analysis was produced."
        )
    if message.stop_reason == "max_tokens":
        raise AnalysisError(
            "The response hit the token limit before finishing. Try a lower "
            "effort level."
        )

    text = "".join(b.text for b in message.content if b.type == "text")
    if not text.strip():
        raise AnalysisError("The model returned no content.")

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AnalysisError(f"The model returned unparseable JSON: {e}") from e


def analyze_deck_bytes(
    data: bytes,
    effort: str = "high",
    api_key: str | None = None,
    on_progress: Callable[[], None] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> dict:
    """Validate, convert if needed, analyze, and normalize.

    Accepts PDF or PowerPoint bytes. Returns the report-ready dict.
    """
    kind = validate_deck(data)
    client = build_client(api_key)
    if kind != "pdf":
        if on_note:
            on_note("Converting the PowerPoint deck to PDF…")
        data = convert_to_pdf(data, kind)
        if on_note:
            on_note(
                "Converted. Claude is reading the deck — this usually takes "
                "one to three minutes."
            )
    pdf_b64 = base64.standard_b64encode(data).decode("utf-8")
    raw = call_model(client, pdf_b64, effort, on_progress, on_note)
    return cl.normalize(raw)
