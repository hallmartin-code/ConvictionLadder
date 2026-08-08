"""
Conviction Ladder analysis engine.

Shared by the CLI (`analyze.py`) and the web app (`web.py`): takes pitch deck
PDF bytes, calls the Claude API with a structured-output schema, and returns a
normalized analysis dict ready for `report_pdf.build_report`.
"""

from __future__ import annotations

import base64
import json
from typing import Callable

import anthropic

import conviction_ladder as cl

# Load .env for local dev if python-dotenv is installed. On Railway the real
# environment already holds the variables, and there is no .env to find.
try:
    from dotenv import load_dotenv

    load_dotenv()
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


class AnalysisError(RuntimeError):
    """Raised for problems the caller should surface to a user verbatim."""


def validate_pdf(data: bytes, filename: str = "deck.pdf") -> None:
    """Reject obvious non-PDFs and oversized uploads before spending a call."""
    if not data:
        raise AnalysisError("The uploaded file is empty.")
    if not data.startswith(b"%PDF"):
        raise AnalysisError(
            f"{filename} does not look like a PDF. Export the deck to PDF and "
            "try again."
        )
    if len(data) > MAX_PDF_BYTES:
        mb = len(data) / (1024 * 1024)
        raise AnalysisError(
            f"{filename} is {mb:.1f}MB. The limit is "
            f"{MAX_PDF_BYTES // (1024 * 1024)}MB — re-export the deck at lower "
            "image quality and try again."
        )


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


def analyze_pdf_bytes(
    data: bytes,
    effort: str = "high",
    api_key: str | None = None,
    on_progress: Callable[[], None] | None = None,
    on_note: Callable[[str], None] | None = None,
) -> dict:
    """Validate, analyze, and normalize. Returns the report-ready dict."""
    validate_pdf(data)
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    pdf_b64 = base64.standard_b64encode(data).decode("utf-8")
    raw = call_model(client, pdf_b64, effort, on_progress, on_note)
    return cl.normalize(raw)
