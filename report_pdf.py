"""
Renders a conviction analysis dict into the one-page Investor Conviction
Ladder PDF: header bar, score bar, stage summary table, deal momentum
diagnosis, and a 5x2 grid of stage cards.

The report is meant to be a single page. Model output length isn't fixed, so
`build_report` renders at progressively smaller body type until it fits on one
page (see FIT_SCALES) rather than silently spilling onto a second page or
clipping text mid-word.
"""

from __future__ import annotations

import io
import unicodedata
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

# --- Palette ------------------------------------------------------------------

HEADER_BG = colors.HexColor("#16283A")   # top bar
SECTION_BG = colors.HexColor("#2C5F8D")  # section + table-header bars
LABEL_FG = colors.HexColor("#2C5F8D")    # diagnosis labels, card titles
BOX_BG = colors.HexColor("#EEF2F6")      # light panel fill
GRID = colors.HexColor("#C3CDD8")
BODY_FG = colors.HexColor("#1A1A1A")
MUTED_FG = colors.HexColor("#6B7683")

SCORE_COLORS = {
    5: colors.HexColor("#1E7A3C"),
    4: colors.HexColor("#1E7A3C"),
    3: colors.HexColor("#2C5F8D"),
    2: colors.HexColor("#D2691E"),
    1: colors.HexColor("#B3261E"),
}

VERDICT_COLORS = {
    "Strong Conviction": colors.HexColor("#1E7A3C"),
    "Advance with Conditions": colors.HexColor("#2C5F8D"),
    "Borderline": colors.HexColor("#E8833A"),
    "Pass": colors.HexColor("#B3261E"),
}

# --- Geometry -----------------------------------------------------------------

PAGE_W, PAGE_H = letter
MARGIN = 0.4 * inch
CONTENT_W = PAGE_W - 2 * MARGIN          # 554.4pt

LEFT_W = 200.0                            # stage summary table
GUTTER = 10.0
RIGHT_W = CONTENT_W - LEFT_W - GUTTER     # diagnosis panel
DIAG_LABEL_W = 108.0

CARD_COLS = 5
CARD_W = CONTENT_W / CARD_COLS

# Body-type sizes tried in order; the first that fits on one page wins.
# Below the last value the report stops being readable, so at that point we
# accept a two-page render rather than shrink further.
FIT_SCALES = [5.3, 5.0, 4.8, 4.6, 4.4, 4.2]

# --- Styles -------------------------------------------------------------------


def _style(name, **kw):
    base = dict(fontName="Helvetica", fontSize=6, leading=7.4, textColor=BODY_FG)
    base.update(kw)
    return ParagraphStyle(name, **base)


S_HEADER_TITLE = _style("hdr", fontName="Helvetica-Bold", fontSize=11, leading=13,
                        textColor=colors.white)
S_HEADER_CO = _style("hdrco", fontName="Helvetica-Bold", fontSize=11, leading=13,
                     textColor=colors.white, alignment=TA_CENTER)
S_HEADER_DATE = _style("hdrdate", fontSize=8, leading=13, textColor=colors.white,
                       alignment=2)

S_SECTION = _style("section", fontName="Helvetica-Bold", fontSize=8, leading=10,
                   textColor=colors.white)
S_SECTION_C = _style("sectionc", fontName="Helvetica-Bold", fontSize=8, leading=10,
                     textColor=colors.white, alignment=TA_CENTER)

S_SCORE_LABEL = _style("scorelbl", fontName="Helvetica-Bold", fontSize=8.5, leading=11)
S_SCORE_VALUE = _style("scoreval", fontName="Helvetica-Bold", fontSize=10.5, leading=13,
                       textColor=colors.white, alignment=TA_CENTER)

S_TH = _style("th", fontName="Helvetica-Bold", fontSize=6.5, leading=8,
              textColor=colors.white)
S_TH_C = _style("thc", fontName="Helvetica-Bold", fontSize=6.5, leading=8,
                textColor=colors.white, alignment=TA_CENTER)
S_TD = _style("td", fontSize=6.5, leading=8)
S_TD_NUM = _style("tdnum", fontSize=6.5, leading=8, alignment=TA_CENTER,
                  textColor=MUTED_FG)

S_FOOTER = _style("footer", fontSize=6, leading=8, textColor=MUTED_FG,
                  alignment=TA_CENTER)


# The built-in Type 1 fonts (Helvetica et al.) are limited to WinAnsi, which
# is cp1252. Model output regularly contains characters outside it - "!=" as
# U+2260, arrows, math operators - and reportlab's handling of those is not
# well defined, so map them to equivalents the base fonts can actually draw.
_TRANSLITERATE = {
    "≠": "!=", "≤": "<=", "≥": ">=", "≈": "~",
    "≡": "=", "∞": "inf", "∑": "sum", "√": "sqrt",
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>",
    "↑": "up", "↓": "down",
    "′": "'", "″": '"', "‐": "-", "‑": "-",
    "−": "-", " ": " ", " ": " ", " ": " ",
    "–": "–", "—": "—",  # en/em dash are in cp1252
}


def to_font_safe(text: str) -> str:
    """Reduce text to characters the built-in PDF fonts can render."""
    out = []
    for char in text:
        replacement = _TRANSLITERATE.get(char)
        if replacement is not None:
            out.append(replacement)
            continue
        try:
            char.encode("cp1252")
        except UnicodeEncodeError:
            # Strip accents and expand ligatures where possible; anything with
            # no Latin equivalent (CJK, symbols) becomes a visible '?' rather
            # than silently disappearing from the report.
            folded = unicodedata.normalize("NFKD", char)
            folded = folded.encode("ascii", "ignore").decode("ascii")
            out.append(folded or "?")
        else:
            out.append(char)
    return "".join(out)


def esc(text) -> str:
    """Make model text safe for both the PDF fonts and reportlab's XML parser."""
    return escape(to_font_safe("" if text is None else str(text)))


def _bar(cells, widths, bg, extra=None):
    """A single-row full-bleed bar."""
    style = [
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if extra:
        style.extend(extra)
    table = Table([cells], colWidths=widths)
    table.setStyle(TableStyle(style))
    return table


def _flush(rows):
    """Zero out cell padding on a layout-only wrapper table."""
    return TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ])


def _header(analysis: dict, report_date: str):
    return _bar(
        [
            Paragraph("INVESTOR CONVICTION LADDER", S_HEADER_TITLE),
            Paragraph(esc(analysis["company_name"]), S_HEADER_CO),
            Paragraph(esc(report_date), S_HEADER_DATE),
        ],
        [200.0, CONTENT_W - 200.0 - 130.0, 130.0],
        HEADER_BG,
        extra=[("TOPPADDING", (0, 0), (-1, -1), 7),
               ("BOTTOMPADDING", (0, 0), (-1, -1), 7)],
    )


def _score_row(analysis: dict):
    verdict = analysis["verdict"]
    bar_color = VERDICT_COLORS.get(verdict, SECTION_BG)
    text = f"{analysis['overall_score']} / 50 &mdash; {esc(verdict)}"

    left = _bar([Paragraph("Overall Score", S_SCORE_LABEL)], [LEFT_W], BOX_BG,
                extra=[("BOX", (0, 0), (-1, -1), 0.5, GRID),
                       ("TOPPADDING", (0, 0), (-1, -1), 6),
                       ("BOTTOMPADDING", (0, 0), (-1, -1), 6)])
    right = _bar([Paragraph(text, S_SCORE_VALUE)], [RIGHT_W], bar_color,
                 extra=[("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6)])

    outer = Table([[left, "", right]], colWidths=[LEFT_W, GUTTER, RIGHT_W])
    outer.setStyle(_flush(outer))
    return outer


def _stage_table(analysis: dict):
    rows = [[
        Paragraph("#", S_TH_C),
        Paragraph("Stage", S_TH),
        Paragraph("Score", S_TH_C),
        Paragraph("Status", S_TH_C),
    ]]
    for stage in analysis["stages"]:
        tint = SCORE_COLORS.get(stage["score"], BODY_FG)
        score_style = _style(f"sc{stage['number']}", fontName="Helvetica-Bold",
                             fontSize=6.5, leading=8, alignment=TA_CENTER,
                             textColor=tint)
        status_style = _style(f"st{stage['number']}", fontSize=6.5, leading=8,
                              alignment=TA_CENTER, textColor=tint)
        rows.append([
            Paragraph(str(stage["number"]), S_TD_NUM),
            Paragraph(esc(stage["name"]), S_TD),
            Paragraph(f"{stage['score']}/5", score_style),
            Paragraph(esc(stage["status"]), status_style),
        ])

    table = Table(rows, colWidths=[18, 112, 32, 38])
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), SECTION_BG),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for i in range(1, len(rows)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), BOX_BG))
    table.setStyle(TableStyle(style))
    return table


DIAG_FIELDS = [
    ("conviction_breaks_at", "Conviction breaks at:"),
    ("partner_meeting", "Partner Meeting?"),
    ("biggest_red_flag", "Biggest Red Flag:"),
    ("highest_leverage", "Highest Leverage:"),
    ("increase_p_invest", "To increase P(invest):"),
]


def _diagnosis(analysis: dict, size: float):
    diag = analysis["diagnosis"]
    lead = size * 1.28
    label_style = _style("dlabel", fontName="Helvetica-Bold", fontSize=size,
                         leading=lead, textColor=LABEL_FG)
    value_style = _style("dvalue", fontSize=size, leading=lead)

    rows = [[Paragraph("DEAL MOMENTUM DIAGNOSIS", S_SECTION_C), ""]]
    for key, label in DIAG_FIELDS:
        value = diag.get(key, "—")
        style = value_style
        if key == "partner_meeting":
            style = _style(
                "pm", fontName="Helvetica-Bold", fontSize=size, leading=lead,
                textColor=colors.HexColor("#1E7A3C")
                if value in ("Yes", "Likely") else colors.HexColor("#B3261E"),
            )
        rows.append([Paragraph(esc(label), label_style),
                     Paragraph(esc(value), style)])

    table = Table(rows, colWidths=[DIAG_LABEL_W, RIGHT_W - DIAG_LABEL_W])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SECTION_BG),
        ("SPAN", (0, 0), (-1, 0)),
        ("BOX", (0, 1), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _top_block(analysis: dict, size: float):
    outer = Table(
        [[_stage_table(analysis), "", _diagnosis(analysis, size)]],
        colWidths=[LEFT_W, GUTTER, RIGHT_W],
    )
    outer.setStyle(_flush(outer))
    return outer


def _card(stage: dict, size: float):
    lead = size * 1.23
    title_style = _style(f"ct{stage['number']}", fontName="Helvetica-Bold",
                         fontSize=size + 0.5, leading=(size + 0.5) * 1.2,
                         textColor=SCORE_COLORS.get(stage["score"], LABEL_FG))
    body = _style("cbody", fontSize=size, leading=lead)
    question = _style("cq", fontName="Helvetica-Oblique", fontSize=size,
                      leading=lead)
    gap = max(1.0, size * 0.28)
    return [
        Paragraph(
            f"S{stage['number']}: {esc(stage['name'])} [{stage['score']}/5]",
            title_style,
        ),
        Spacer(1, gap * 1.6),
        Paragraph(f"<b>Evidence:</b> {esc(stage['evidence'])}", body),
        Spacer(1, gap),
        Paragraph(f"<b>Gap:</b> {esc(stage['gap'])}", body),
        Spacer(1, gap),
        Paragraph(f"<b>Q:</b> {esc(stage['question'])}", question),
        Spacer(1, gap),
        Paragraph(f"<b>Fix:</b> {esc(stage['fix'])}", body),
    ]


def _card_grid(analysis: dict, size: float):
    stages = analysis["stages"]
    rows = [
        [_card(s, size) for s in stages[0:5]],
        [_card(s, size) for s in stages[5:10]],
    ]
    table = Table(rows, colWidths=[CARD_W] * CARD_COLS)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 1), (-1, 1), BOX_BG),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _render(analysis: dict, report_date: str, target, size: float) -> int:
    """Render at the given body-type size. Returns the page count."""
    footer_text = (
        f"Generated by Investor Conviction Ladder Analyzer &middot; "
        f"{esc(report_date)} &middot; Confidential"
    )

    def draw_footer(canvas, doc):
        canvas.saveState()
        para = Paragraph(footer_text, S_FOOTER)
        para.wrap(CONTENT_W, 20)
        para.drawOn(canvas, MARGIN, MARGIN * 0.6)
        canvas.restoreState()

    doc = BaseDocTemplate(
        target,
        pagesize=letter,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN + 12,
        title=f"Investor Conviction Ladder — {analysis['company_name']}",
        author="TEN Capital Network",
    )
    frame = Frame(
        MARGIN, doc.bottomMargin, CONTENT_W,
        PAGE_H - MARGIN - doc.bottomMargin,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    doc.addPageTemplates([PageTemplate(id="report", frames=[frame],
                                       onPage=draw_footer)])

    doc.build([
        _header(analysis, report_date),
        Spacer(1, 8),
        _score_row(analysis),
        Spacer(1, 8),
        _top_block(analysis, min(size + 1.2, 6.5)),
        Spacer(1, 10),
        _bar([Paragraph("STAGE-BY-STAGE ANALYSIS", S_SECTION)], [CONTENT_W],
             SECTION_BG),
        Spacer(1, 6),
        _card_grid(analysis, size),
    ])
    return doc.page


def render_report_bytes(analysis: dict, report_date: str) -> bytes:
    """Render the conviction report, shrinking body type until it fits a page."""
    best = b""
    for size in FIT_SCALES:
        buffer = io.BytesIO()
        pages = _render(analysis, report_date, buffer, size)
        best = buffer.getvalue()
        if pages == 1:
            break
    return best


def build_report(analysis: dict, output_path: Path, report_date: str) -> Path:
    """Write the conviction report to disk."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(render_report_bytes(analysis, report_date))
    return output_path
