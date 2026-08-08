"""
Investor Conviction Ladder — pitch deck analyzer (CLI).

    Upload PDF -> score 10 conviction stages -> one-page branded PDF report

Usage
-----
    python analyze.py your_deck.pdf
    python analyze.py deck.pdf --output "Company - Conviction Ladder.pdf"
    python analyze.py deck.pdf --json analysis.json
    python analyze.py deck.pdf --effort max
    python analyze.py --from-json analysis.json      # re-render, no API call

Requirements
------------
    pip install -r requirements.txt

Authentication
--------------
The Anthropic SDK resolves credentials from the environment automatically:
set ANTHROPIC_API_KEY, or run `ant auth login` once. You do NOT need to pass a
key in code.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import anthropic

import conviction_ladder as cl
import engine
from report_pdf import build_report

# Model output contains typography the Windows console codepage can't encode
# (em dashes, "!=" as U+2260). Without this, printing the summary raises
# UnicodeEncodeError after the report has already been written.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def run(pdf_path: Path | None, output_path: Path, json_path: Path | None,
        effort: str, from_json: Path | None) -> None:
    if from_json:
        analysis = cl.normalize(json.loads(from_json.read_text(encoding="utf-8")))
    else:
        print("Scoring 10 conviction stages ", end="", file=sys.stderr, flush=True)
        analysis = engine.analyze_pdf_bytes(
            pdf_path.read_bytes(),
            effort=effort,
            on_progress=lambda: print(".", end="", file=sys.stderr, flush=True),
            on_note=lambda m: print(f"\nNote: {m}", file=sys.stderr),
        )
        print(" done.", file=sys.stderr, flush=True)

    report_date = date.today().strftime("%B %d, %Y")

    if json_path:
        json_path.write_text(json.dumps(analysis, indent=2), encoding="utf-8")
        print(f"Saved analysis JSON  -> {json_path}")

    build_report(analysis, output_path, report_date)

    print(
        f"\n{analysis['company_name']}: "
        f"{analysis['overall_score']} / {cl.MAX_SCORE} — {analysis['verdict']}"
    )
    print(f"Conviction breaks at: {analysis['diagnosis']['conviction_breaks_at']}")
    print(f"\nSaved report -> {output_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Score a pitch deck against the TEN Capital Investor Conviction "
            "Ladder and produce a one-page PDF report."
        )
    )
    parser.add_argument(
        "pdf", type=Path, nargs="?",
        help="Path to the pitch deck PDF (omit only with --from-json)",
    )
    parser.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output .pdf path (default: <deck name> - Conviction Ladder.pdf)",
    )
    parser.add_argument(
        "--from-json", type=Path, default=None, dest="from_json",
        help="Re-render a report from a saved analysis JSON; makes no API call",
    )
    parser.add_argument(
        "--json", type=Path, default=None, dest="json_path",
        help="Also write the raw analysis as JSON to this path",
    )
    parser.add_argument(
        "--effort", default="high", choices=engine.EFFORT_LEVELS,
        help="Model effort level (default: high)",
    )
    args = parser.parse_args(argv)

    if args.from_json:
        if not args.from_json.exists():
            parser.error(f"File not found: {args.from_json}")
        default_out = args.from_json.with_suffix(".pdf")
    else:
        if args.pdf is None:
            parser.error("a pitch deck PDF is required (or pass --from-json)")
        if not args.pdf.exists():
            parser.error(f"File not found: {args.pdf}")
        if args.pdf.suffix.lower() != ".pdf":
            parser.error("Input must be a .pdf file")
        default_out = args.pdf.with_name(
            f"{args.pdf.stem} - Conviction Ladder.pdf"
        )

    output = args.output or default_out

    try:
        run(args.pdf, output, args.json_path, args.effort, args.from_json)
    except anthropic.AuthenticationError:
        # A missing key never gets this far — engine.build_client turns that
        # into an AnalysisError. This is the 401: a key that is set but wrong,
        # revoked, or from the wrong workspace.
        print(
            "\nAnthropic rejected the credentials (401). The key in "
            "ANTHROPIC_API_KEY is invalid, revoked, or truncated.\n"
            "Check it at https://console.anthropic.com/settings/keys",
            file=sys.stderr,
        )
        return 1
    except anthropic.APIError as e:
        print(f"\nAPI error: {e}", file=sys.stderr)
        return 1
    except (engine.AnalysisError, ValueError) as e:
        print(f"\n{e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
