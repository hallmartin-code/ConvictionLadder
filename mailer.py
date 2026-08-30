"""
Emails a finished conviction report via Resend.

Configured entirely from the environment, and inert when RESEND_API_KEY is
unset — so local runs and any deployment that hasn't opted in simply skip the
send rather than failing.

Environment
-----------
RESEND_API_KEY      enables sending. Get one at resend.com/api-keys
REPORT_EMAIL_TO     recipients, comma-separated. Default Info@tencapital.group
REPORT_EMAIL_FROM   sender. MUST be on a domain verified in Resend, otherwise
                    the API rejects the send. Default uses Resend's shared
                    onboarding sender, which only delivers to the address that
                    owns the Resend account — fine for a smoke test, not for
                    production.
REPORT_EMAIL_CC     optional, comma-separated
"""

from __future__ import annotations

import base64
import os
from html import escape

import httpx

RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_TO = "Info@tencapital.group"
DEFAULT_FROM = "Conviction Ladder <onboarding@resend.dev>"

# Resend caps a request at 40MB. Our reports are ~10KB, so this only guards
# against a pathological render.
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024

VERDICT_COLOR = {
    "Strong Conviction": "#1E7A3C",
    "Advance with Conditions": "#2C5F8D",
    "Borderline": "#D2691E",
    "Pass": "#B3261E",
}
SCORE_COLOR = {5: "#1E7A3C", 4: "#1E7A3C", 3: "#2C5F8D", 2: "#D2691E", 1: "#B3261E"}

DIAGNOSIS_FIELDS = [
    ("conviction_breaks_at", "Conviction breaks at"),
    ("partner_meeting", "Partner meeting?"),
    ("biggest_red_flag", "Biggest red flag"),
    ("highest_leverage", "Highest leverage"),
    ("increase_p_invest", "To increase P(invest)"),
]


class MailError(RuntimeError):
    """Raised when a send is attempted and fails. Never fatal to a report."""


def _recipients(raw: str | None, fallback: str = "") -> list[str]:
    return [a.strip() for a in (raw or fallback).split(",") if a.strip()]


def is_configured() -> bool:
    return bool((os.getenv("RESEND_API_KEY") or "").strip())


def recipients() -> list[str]:
    return _recipients(os.getenv("REPORT_EMAIL_TO"), DEFAULT_TO)


def sender() -> str:
    return (os.getenv("REPORT_EMAIL_FROM") or "").strip() or DEFAULT_FROM


def build_subject(analysis: dict) -> str:
    return (
        f"Conviction Ladder — {analysis['company_name']}: "
        f"{analysis['overall_score']}/50 {analysis['verdict']}"
    )


def build_html(analysis: dict, report_date: str, source_filename: str) -> str:
    """A readable summary for the inbox; the PDF rides along as an attachment."""
    verdict = analysis["verdict"]
    tint = VERDICT_COLOR.get(verdict, "#2C5F8D")

    rows = []
    for stage in analysis["stages"]:
        color = SCORE_COLOR.get(stage["score"], "#1A1A1A")
        rows.append(
            '<tr>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #E3E9F0;'
            f'color:#6B7683;font-size:12px;">{stage["number"]}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #E3E9F0;'
            f'font-size:13px;">{escape(stage["name"])}</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #E3E9F0;'
            f'text-align:center;font-weight:bold;font-size:13px;color:{color};">'
            f'{stage["score"]}/5</td>'
            f'<td style="padding:6px 10px;border-bottom:1px solid #E3E9F0;'
            f'text-align:right;font-size:12px;color:{color};">'
            f'{escape(stage["status"])}</td>'
            '</tr>'
        )

    diagnosis = []
    for key, label in DIAGNOSIS_FIELDS:
        value = analysis["diagnosis"].get(key) or "—"
        diagnosis.append(
            f'<p style="margin:0 0 14px;"><span style="display:block;'
            f'font-size:11px;letter-spacing:.08em;text-transform:uppercase;'
            f'color:#2C5F8D;font-weight:bold;margin-bottom:3px;">'
            f'{escape(label)}</span>'
            f'<span style="font-size:13px;line-height:1.55;color:#1A1A1A;">'
            f'{escape(str(value))}</span></p>'
        )

    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#F2F5F8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="background:#F2F5F8;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0"
       style="max-width:640px;background:#FFFFFF;border:1px solid #D6DEE8;
              border-radius:8px;overflow:hidden;
              font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;">

  <tr><td style="background:#16283A;padding:16px 22px;">
    <span style="color:#FFFFFF;font-size:13px;font-weight:bold;
                 letter-spacing:.06em;">INVESTOR CONVICTION LADDER</span>
  </td></tr>

  <tr><td style="padding:22px 22px 6px;">
    <span style="font-size:34px;font-weight:bold;color:{tint};">
      {analysis['overall_score']}</span>
    <span style="font-size:14px;color:#6B7683;"> / 50</span>
    <span style="display:inline-block;margin-left:10px;padding:4px 11px;
                 border-radius:14px;background:{tint};color:#FFFFFF;
                 font-size:11px;font-weight:bold;letter-spacing:.05em;
                 text-transform:uppercase;">{escape(verdict)}</span>
    <div style="margin-top:8px;font-size:16px;font-weight:bold;color:#1A1A1A;">
      {escape(analysis['company_name'])}</div>
    <div style="margin-top:2px;font-size:12px;color:#6B7683;">
      {escape(report_date)} &middot; from {escape(source_filename)}</div>
  </td></tr>

  <tr><td style="padding:16px 22px 0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;">
      <tr style="background:#2C5F8D;">
        <th style="padding:6px 10px;text-align:left;color:#FFFFFF;
                   font-size:10px;letter-spacing:.1em;">#</th>
        <th style="padding:6px 10px;text-align:left;color:#FFFFFF;
                   font-size:10px;letter-spacing:.1em;">STAGE</th>
        <th style="padding:6px 10px;text-align:center;color:#FFFFFF;
                   font-size:10px;letter-spacing:.1em;">SCORE</th>
        <th style="padding:6px 10px;text-align:right;color:#FFFFFF;
                   font-size:10px;letter-spacing:.1em;">STATUS</th>
      </tr>
      {''.join(rows)}
    </table>
  </td></tr>

  <tr><td style="padding:22px;">
    <div style="border-top:1px solid #D6DEE8;padding-top:18px;">
      {''.join(diagnosis)}
    </div>
  </td></tr>

  <tr><td style="background:#F7F9FB;border-top:1px solid #D6DEE8;
                 padding:14px 22px;font-size:11px;color:#6B7683;">
    The full one-page report is attached as a PDF.
    Generated automatically by the Investor Conviction Ladder analyzer.
    Confidential.
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def send_report(
    analysis: dict,
    pdf: bytes,
    report_date: str,
    source_filename: str = "deck.pdf",
    attachment_name: str | None = None,
    timeout: float = 20.0,
) -> str:
    """Email the report. Returns a short status string for logging/UI.

    Raises MailError on any failure. Callers must treat that as non-fatal —
    the report itself already exists and the user is waiting on it.
    """
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not key:
        raise MailError("RESEND_API_KEY is not set.")

    to = recipients()
    if not to:
        raise MailError("REPORT_EMAIL_TO is empty; nowhere to send.")
    if len(pdf) > MAX_ATTACHMENT_BYTES:
        raise MailError(f"Report attachment is {len(pdf) / 1048576:.1f}MB; too large to email.")

    name = attachment_name or (
        f"{analysis['company_name']} - Conviction Ladder.pdf"
    )

    payload = {
        "from": sender(),
        "to": to,
        "subject": build_subject(analysis),
        "html": build_html(analysis, report_date, source_filename),
        "attachments": [
            {
                "filename": name,
                "content": base64.standard_b64encode(pdf).decode("ascii"),
            }
        ],
    }
    cc = _recipients(os.getenv("REPORT_EMAIL_CC"))
    if cc:
        payload["cc"] = cc

    try:
        response = httpx.post(
            RESEND_ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise MailError(f"Could not reach Resend: {e}") from e

    if response.status_code >= 400:
        raise MailError(_explain(response))

    return f"Emailed to {', '.join(to)}"


def _explain(response: httpx.Response) -> str:
    """Turn a Resend error into something actionable rather than a status code."""
    try:
        detail = response.json().get("message") or response.text
    except ValueError:
        detail = response.text
    detail = (detail or "").strip()[:400]

    if response.status_code == 401:
        return f"Resend rejected the API key (401). {detail}"
    if response.status_code == 403:
        return (
            f"Resend refused the sender {sender()!r} (403). The From domain has "
            f"to be verified at resend.com/domains. {detail}"
        )
    if response.status_code == 422:
        return (
            f"Resend rejected the message (422). This is usually an unverified "
            f"From domain, or the shared onboarding@resend.dev sender being "
            f"used for an address other than the Resend account owner. {detail}"
        )
    if response.status_code == 429:
        return f"Resend rate limit hit (429). {detail}"
    return f"Resend returned {response.status_code}. {detail}"
