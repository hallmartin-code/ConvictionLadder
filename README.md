# Investor Conviction Ladder Analyzer

Scores a pitch deck against the TEN Capital Investor Conviction Ladder and
produces a one-page PDF report: ten stage scores out of 50, a deal momentum
diagnosis, and per-stage evidence / gap / question / fix.

Two front ends over the same engine:

- **Web app** (`web.py`) — upload a deck in the browser, download the report.
  Deploys to Railway.
- **CLI** (`analyze.py`) — same thing from the terminal.

---

## Get an API key

1. Go to <https://console.anthropic.com/settings/keys> and create a key.
2. Keep it server-side. It is never sent to the browser, and the web app has
   no field to paste one into — that's deliberate.

---

## Run the web app locally

```powershell
pip install -r requirements.txt
copy .env.example .env      # then put your key in .env
uvicorn web:app --reload
```

`.env` is gitignored and loaded automatically. If you'd rather not use a file,
`$env:ANTHROPIC_API_KEY = "sk-ant-..."` works too — a real environment variable
takes precedence over `.env`.

Open <http://127.0.0.1:8000>. Drop in a deck, pick an effort level, run it.
The page polls for progress and then offers the PDF and the raw JSON.

---

## Deploy to Railway

The repo already contains everything Railway needs: `Procfile`,
`railway.json`, `requirements.txt`, and `.python-version`.

1. **Push this folder to its own GitHub repo.** It must be the repo root —
   Railway looks for `requirements.txt` at the top level.

   ```powershell
   cd "ConvictionLadder"
   git init
   git add .
   git commit -m "Investor Conviction Ladder analyzer"
   git branch -M main
   git remote add origin https://github.com/<you>/conviction-ladder.git
   git push -u origin main
   ```

2. **Create the service.** In Railway: *New Project → Deploy from GitHub repo
   →* pick the repo. Nixpacks detects Python, installs `requirements.txt`, and
   runs the `Procfile` start command.

3. **Set the variables.** Service → *Variables* → add:

   | Variable | Value |
   | --- | --- |
   | `ANTHROPIC_API_KEY` | your key from the Claude console |
   | `APP_PASSWORD` | a long random string — see the warning below |
   | `APP_USERNAME` | optional, defaults to `ten` |
   | `RESEND_API_KEY` | optional — enables emailing each report |
   | `REPORT_EMAIL_TO` | optional, defaults to `Info@tencapital.group` |
   | `REPORT_EMAIL_FROM` | required if emailing; must be on a Resend-verified domain |

   Railway injects `PORT` itself; don't set it.

4. **Generate a domain.** Service → *Settings → Networking → Generate Domain*.

5. **Check it.** Visit `https://<your-app>.up.railway.app/healthz` — it should
   report `"api_key_configured": true` and `"auth_enabled": true`. Then open
   the root URL; the browser will prompt for the username and password.

### ⚠️ Set `APP_PASSWORD` before you share the URL

A Railway domain is public. Without `APP_PASSWORD` the app is an open endpoint
that spends your Anthropic budget for anyone who finds it, and Opus 5 analysis
of a full deck is not cheap. The app runs fine without it — it just doesn't
gate anything — so set it as part of the first deploy, not later.

`/healthz` stays open on purpose so Railway's health check can reach it. It
exposes no key material, only booleans.

### Deployment notes

- **Jobs live in memory.** A submitted analysis and its finished report are
  held in the process, and dropped after `JOB_TTL_SECONDS` (default 1 hour). A
  redeploy or crash loses in-flight jobs. Keep `numReplicas` at 1 — with two
  replicas, polling can hit an instance that never saw the job.
- **Concurrency** is capped by `MAX_CONCURRENT` (default 2). Raise it only if
  your Anthropic rate limits and Railway memory allow.
- **No long HTTP requests.** Upload returns a job ID immediately and the
  browser polls, so nothing sits near a proxy timeout even on a `max`-effort
  run.
- **Decks are not persisted.** Upload bytes are held only for the duration of
  the analysis; nothing is written to disk.

---

## CLI

```
python analyze.py deck.pdf                          # analyze and render
python analyze.py deck.pdf -o "Report.pdf"          # choose the output path
python analyze.py deck.pdf --json analysis.json     # also save the raw scores
python analyze.py deck.pdf --effort max             # deepest analysis, slower
python analyze.py --from-json analysis.json         # re-render, no API call
```

`--effort` accepts `low | medium | high | xhigh | max` (default `high`).

`--from-json` re-renders a saved analysis without calling the API — useful for
tweaking layout, or regenerating a report after editing scores by hand. Try it
with the bundled fixture, no key required:

```powershell
python analyze.py --from-json sample_analysis.json
```

---

## HTTP API

Every endpoint except `/healthz` is behind the same auth gate.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Upload page |
| `POST` | `/api/analyze` | multipart: `deck` (PDF), `effort`. Returns `202 {"id"}` |
| `GET` | `/api/jobs/{id}` | Status, and on completion the scores and diagnosis |
| `GET` | `/api/jobs/{id}/report` | The PDF report |
| `GET` | `/api/jobs/{id}/analysis` | The raw analysis JSON |
| `GET` | `/healthz` | Liveness + whether the key and auth are configured |

```powershell
curl -u ten:$env:APP_PASSWORD -F "deck=@deck.pdf" -F "effort=high" `
  https://<your-app>.up.railway.app/api/analyze
```

---

## Files

| File | Purpose |
| --- | --- |
| `web.py` | FastAPI app and job queue |
| `index.html` | The upload page — brand, all four UI states, styling |
| `static/` | Favicon set and brand assets, served at `/static` |
| `analyze.py` | CLI |
| `engine.py` | Shared core: validation, the API call, normalization |
| `conviction_ladder.py` | Stage definitions, scoring rubric, prompt, JSON schema |
| `report_pdf.py` | The one-page PDF layout |
| `mailer.py` | Emails the finished report via Resend |
| `sample_analysis.json` | Fixture for testing the renderer without an API key |
| `Procfile`, `railway.json`, `.python-version` | Railway deployment |

## Emailing reports

Set `RESEND_API_KEY` and every finished analysis is emailed automatically: a
summary in the body (score, verdict, the ten stage scores, the full diagnosis)
with the one-page PDF attached. Leave the key unset and the feature is inert.

| Variable | Purpose |
| --- | --- |
| `RESEND_API_KEY` | Enables sending. From <https://resend.com/api-keys> |
| `REPORT_EMAIL_TO` | Recipients, comma-separated. Default `Info@tencapital.group` |
| `REPORT_EMAIL_FROM` | Sender. **Must be on a domain verified at <https://resend.com/domains>** |
| `REPORT_EMAIL_CC` | Optional, comma-separated |

`tencapital.group` is verified, so `reports@tencapital.group` sends fine — the
address needs no mailbox, only the verified domain. Resend's shared
`onboarding@resend.dev` sender only delivers to the Resend account owner, so
it is not usable for team delivery.

**Emailing never blocks a report.** The send runs after the job is marked done
and the PDF is already downloadable. A failure is recorded on the job, shown
in the UI, and logged to stderr — the analysis is never lost to a mail problem.

On the CLI it is opt-in via `--email`, so local experiments don't mail the team
on every run.

## Brand assets

`static/` holds the favicon set, generated from the TEN Capital circle mark:

| File | Used for |
| --- | --- |
| `favicon.ico` | Multi-resolution (16/32/48) for browser tabs and Windows |
| `favicon-16.png`, `favicon-32.png` | Modern browser tab icons |
| `apple-touch-icon.png` | iOS home screen, 180×180 on the app's navy |
| `icon-512.png`, `favicon-192.png` | Large sizes, e.g. a web manifest |
| `logo-source.png` | The trimmed, squared master the rest were cut from |

It's mounted at `/static` **without** the auth gate — the browser fetches the
favicon before you've logged in, and gating it just yields a blank tab icon on
the password prompt. Nothing in the directory is sensitive. `/favicon.ico` is
also served from the root, for clients that probe there instead of reading the
`<link>` tags.

To regenerate from a new master, trim to the artwork's bounding box before
downscaling — the source PNG carries transparent padding that swallows the
glyph at 16px.

## Editing the UI

The page is `index.html`, loaded once at startup by `web.py`. Edit it and
restart — no Python changes needed. Two placeholders are substituted at load:
`{{MAX_BYTES}}` and `{{MAX_MB}}`, both derived from `engine.MAX_PDF_BYTES`, so
the size limit in the copy can never drift from the one the server enforces.

It has four states, swapped by the `view()` helper: `view-upload`,
`view-working`, `view-error`, `view-result`. Each is a `.card` inside
`#stage`; only one is visible at a time.

Design tokens are CSS custom properties on `:root` — navy surfaces, and
coral/amber/teal accents matching the TEN Capital mark. Score and verdict
colours are also mirrored in the JS (`SCORE_COLOR`, `VERDICT_COLOR`); change
both together.

Note the printed PDF keeps its own light palette. It's a document meant to be
read and forwarded, not a screen, and it matches the report format already in
use.

## How it works

The deck is sent as a PDF document block to Claude Opus 5 with a
[structured output schema](conviction_ladder.py), so the model returns
validated JSON rather than prose that needs parsing. The response is checked
for all ten stages, sorted into canonical order, totalled, and rendered by
`report_pdf.py`.

## Notes

- **Scoring rubric.** The stage definitions and per-stage deduction guide in
  `conviction_ladder.py` are taken from the `ten-capital-conviction` plugin, so
  the analyzer scores against the same rubric as the manual workflow.
- **Verdict bands.** 43–50 Strong Conviction · 36–42 Advance with Conditions ·
  28–35 Borderline · ≤27 Pass.
- **One page, always.** The renderer shrinks body type until the report fits on
  a single page, so a verbose analysis is never clipped mid-word or spilled
  onto page two.
- **Upload limit** is 20MB — Anthropic caps a request at 32MB and base64
  inflates by a third. Oversized or non-PDF uploads are rejected before any
  API call is made.
- **Grounding.** The prompt forbids importing outside market knowledge and
  requires evidence to cite what the deck actually shows. Run the Validation
  Check prompt from the parent `CLAUDE.md` for a hallucination audit of a
  specific report.
- **Refusal fallbacks.** Requests opt into Anthropic's server-side fallback so
  a safety-classifier decline is re-served rather than failing. If your account
  doesn't have the beta, the engine retries without it automatically.
- **Typography.** The report uses the built-in PDF fonts, which cover Latin-1
  only. Model output outside that set is transliterated before rendering
  (`≠` → `!=`, `→` → `->`, `≤` → `<=`, accents preserved). Non-Latin scripts —
  CJK, Cyrillic, Arabic — render as `?`; a deck whose company name is in one of
  those needs a Unicode TTF embedded in `report_pdf.py`.

## Keys and secrets

- The key lives in `.env` locally (gitignored) and in Railway's *Variables*
  tab in production. It is never sent to the browser and never written into the
  repo.
- **Never paste a key into a chat, a ticket, or a commit.** If one leaks,
  delete it at <https://console.anthropic.com/settings/keys> and issue a new
  one — rotation is instant and free.
- `.env.example` is the template; it contains no real values and is committed
  on purpose.

## What this tool does not do

It runs the conviction analysis only. The other TEN Capital prompts — Missing
Data readiness review, Red Flag Fast Triage, Validation Check, Founder Q&A,
Deal Memo / IC Brief — live in the parent folder's `CLAUDE.md` and the
`ten-capital-conviction` plugin, and are still run conversationally.
