# ConvictionLadder — project instructions

Python CLI that scores a pitch deck against the TEN Capital Investor Conviction
Ladder and renders a one-page PDF report. The parent
`Claude Cowork folder\CLAUDE.md` also applies and carries the TEN Capital
document standards and the conversational prompt suite.

## Layout

- `engine.py` — shared core: PDF validation, the Anthropic call, normalization
- `web.py` — FastAPI app (upload page + job API), deployed on Railway
- `analyze.py` — CLI, a thin wrapper over `engine`
- `conviction_ladder.py` — stage definitions, deduction rubric, prompt, JSON schema
- `index.html` — the web UI: brand, four view states, all styling
- `static/` — favicon set and brand assets, served at `/static`
- `mailer.py` — Resend delivery of the finished report
- `report_pdf.py` — the reportlab layout
- `sample_analysis.json` — fixture for rendering without an API key

Both front ends go through `engine.analyze_pdf_bytes`. Put analysis logic
there, not in `web.py` or `analyze.py`, so the two stay in step.

## Conventions

- **Model.** `claude-opus-5`, streamed, with adaptive thinking (the default on
  Opus 5) and `output_config.effort`. Don't add `temperature`, `top_p`, or
  `thinking.budget_tokens` — all three are rejected on this model.
- **Structured output, not parsing.** The analysis comes back through
  `output_config.format` against `ANALYSIS_SCHEMA`. If you add a field, add it
  to the schema and to `normalize()`, not to a regex.
- **The rubric is copied, not invented.** Stage definitions and deductions in
  `conviction_ladder.py` mirror `ten-capital-conviction.plugin`. Change them
  there and here together, or the CLI and the conversational workflow will
  diverge.
- **Escape model text before rendering.** Every string that reaches a reportlab
  `Paragraph` goes through `esc()`, which both XML-escapes and reduces the text
  to Latin-1 (`to_font_safe`). An unescaped `&` raises at render time, and the
  built-in PDF fonts can't draw characters outside WinAnsi — the model emits
  `≠` and `→` regularly.
- **Console output must tolerate any codepage.** `analyze.py` reconfigures
  stdout/stderr to UTF-8; without it, printing a summary containing an em dash
  raises `UnicodeEncodeError` on a stock Windows console *after* the report has
  already been written.
- **Secrets live in `.env` (gitignored) or the platform's variables tab.** Never
  hardcode a key, never log one, never accept one from the client.
- **The PDF footer follows the house standard**, not an invented one. The
  pattern, 7pt Open Sans and the 0.67x0.25in logo are specified in the parent
  `CLAUDE.md`; `_draw_ten_footer` implements it. Both assets live in `static/`
  and both have fallbacks, because a missing file must not stop a report.
- **Don't reserve more bottom margin than the footer needs.** The content frame
  stops at `FOOTER_CLEARANCE`, derived from the logo height. Padding it costs
  body copy the space it needs to stay on one page — 10pt of slack was enough
  to push a verbose analysis onto page two.
- **The report is one page.** `build_report` shrinks body type until it fits
  (`FIT_SCALES`). If you add content to the layout, re-run the length stress
  test before assuming it still fits.

## Web app conventions

- **The logo is a raster asset, not inline SVG.** The header mark loads
  `/static/brand-mark.png`. A hand-traced SVG of the TEN Capital circle
  mark rendered wrong (stub arcs, misaligned dots) — don't reintroduce one.

- **`/static` is deliberately unauthenticated.** The favicon is fetched before
  login; gating it only produces a blank tab icon on the password prompt. Keep
  it to brand assets — anything sensitive does not belong there.

- **The UI lives in `index.html`, not in Python.** `web.py` reads it once at
  import and substitutes `{{MAX_BYTES}}`/`{{MAX_MB}}` from
  `engine.MAX_PDF_BYTES`. Never hardcode the upload limit in the copy — it
  would drift from the limit the server actually enforces.
- **Data egress is one explicit, configured destination.** The deck goes to
  Anthropic; the report goes to the browser and, when `RESEND_API_KEY` is set,
  to `REPORT_EMAIL_TO` (a TEN Capital address). Don't add further recipients,
  forwarding, or third-party copies — these are confidential deal documents,
  and the recipient must stay something an operator configured, never something
  hardcoded or inherited from a design asset.
- **Emailing is a side errand, never a gate.** Send after the job is marked
  done and the PDF is downloadable. A `MailError` is recorded on the job and
  logged; it must never fail the analysis the user is waiting on.

- **Never accept an API key from the browser.** The key is server-side only,
  read from `ANTHROPIC_API_KEY`. There is deliberately no field for one.
- **Jobs are in-memory and single-instance.** `numReplicas` stays at 1; polling
  would otherwise hit an instance that never saw the job.
- **Keep requests short.** Upload returns a job ID and the client polls. Don't
  add an endpoint that blocks for the length of an analysis.
- **Hold task references.** Background tasks go in `_TASKS` — asyncio only keeps
  a weak reference, so a bare `create_task` can be collected mid-run.

## Testing without an API key

```powershell
python analyze.py --from-json sample_analysis.json -o out.pdf
```

That exercises the whole render path — schema normalization, scoring totals,
verdict banding, and layout — with no network call. For the web flow, stub
`engine.analyze_pdf_bytes` and drive the app with `fastapi.testclient`; use it
as a context manager (`with TestClient(app) as c:`) or the background job never
finishes, because each request otherwise gets its own short-lived event loop.
