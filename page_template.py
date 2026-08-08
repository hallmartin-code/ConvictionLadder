"""
Landing-page document template — structure only.

`templates/page_template.html` holds the layout, palette, and type scale of the
upload page; every piece of content in it is a `$FIELD` slot. Nothing in the
template names an organization, a person, an address, or a product — supply
those here, per deployment.

    from page_template import render
    html = render(BRAND_NAME="…", HEADLINE_PRIMARY="…", …)

Fields left unset fall back to the neutral placeholders in `DEFAULTS`, so the
template renders standalone for layout work. `string.Template` is used rather
than `str.format` because the CSS is full of literal braces.

Document structure, top to bottom
---------------------------------
    brand lockup      BRAND_MARK_SVG, BRAND_NAME, BRAND_SUBTITLE
    card
      eyebrow         EYEBROW                  — section/kicker label
      headline        HEADLINE_PRIMARY, HEADLINE_ACCENT  — split by an arrow;
                                                 the accent half is gradient-filled
      lede            LEDE                     — one sentence, ≤46ch measure
      dropzone        DROPZONE_TITLE, ACCEPTED_FORMATS, MAX_UPLOAD, ACCEPT_ATTR
      call to action  CTA_LABEL
      disclosure      DISCLOSURE               — what happens to the upload
    footer            FOOTER
"""

from __future__ import annotations

import html
from pathlib import Path
from string import Template

TEMPLATE_PATH = Path(__file__).with_name("templates") / "page_template.html"

# Fields whose values are markup and are inserted verbatim. Everything else is
# HTML-escaped on the way in, so model- or config-supplied text containing an
# `&` or a `<` can't break the page.
RAW_FIELDS = frozenset({"BRAND_MARK_SVG", "ACCEPTED_FORMATS", "DISCLOSURE"})

# A neutral placeholder mark: a rounded frame and one dot per accent. Replace
# with the deployment's own artwork.
PLACEHOLDER_MARK = (
    '<svg class="brand-mark" viewBox="0 0 100 100" fill="none" '
    'xmlns="http://www.w3.org/2000/svg">'
    '<rect x="8" y="8" width="84" height="84" rx="22" '
    'stroke="var(--navy-700)" stroke-width="6"/>'
    '<circle cx="50" cy="30" r="9" fill="var(--accent-2)"/>'
    '<circle cx="68" cy="66" r="9" fill="var(--accent-3)"/>'
    '<circle cx="32" cy="66" r="9" fill="var(--accent-1)"/>'
    "</svg>"
)

DEFAULTS: dict[str, str] = {
    "LANG": "en",
    "PAGE_TITLE": "Document Analyzer",
    "BRAND_MARK_SVG": PLACEHOLDER_MARK,
    "BRAND_NAME": "Organization Name",
    "BRAND_SUBTITLE": "Division",
    "EYEBROW": "Analyzer",
    "HEADLINE_PRIMARY": "Source Document",
    "HEADLINE_ACCENT": "Generated Report",
    "LEDE": "Upload a document and get a structured report back.",
    "DROPZONE_TITLE": "Click to choose a file",
    "ACCEPTED_FORMATS": "",   # filled by format_accepted() when left empty
    "MAX_UPLOAD": "20 MB",  # plain text; the template holds the no-wrap
    "ACCEPT_ATTR": ".pdf",
    "CTA_LABEL": "Generate report",
    "DISCLOSURE": "The uploaded file is processed on the server.",
    "FOOTER": "",             # defaults to BRAND_NAME when left empty
}

# Declared for callers that want to enumerate or validate the slots.
FIELDS: tuple[str, ...] = tuple(DEFAULTS)


def format_accepted(extensions: list[str]) -> str:
    """Render `.pdf · .pptx` as the bolded, middot-separated dropzone line."""
    return " &middot; ".join(f"<b>{html.escape(e)}</b>" for e in extensions)


def render(**fields: str) -> str:
    """Fill the template. Unknown field names raise rather than pass silently."""
    unknown = set(fields) - set(DEFAULTS)
    if unknown:
        raise KeyError(f"Not a field in the template: {sorted(unknown)}")

    values = dict(DEFAULTS)
    values.update({k: v for k, v in fields.items() if v is not None})

    if not values["ACCEPTED_FORMATS"]:
        exts = [e.strip() for e in values["ACCEPT_ATTR"].split(",") if e.strip()]
        values["ACCEPTED_FORMATS"] = format_accepted(exts)
    if not values["FOOTER"]:
        values["FOOTER"] = values["BRAND_NAME"]

    escaped = {
        k: v if k in RAW_FIELDS else html.escape(v, quote=True)
        for k, v in values.items()
    }
    return Template(TEMPLATE_PATH.read_text(encoding="utf-8")).substitute(escaped)


if __name__ == "__main__":  # render the bare template for layout review
    Path("template_preview.html").write_text(render(), encoding="utf-8")
    print("Wrote template_preview.html")
