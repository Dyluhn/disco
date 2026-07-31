"""Blank-render guard for browser-observation content. Extracted verbatim
from `finish/common.py` (module logical LOC reduction); pure, stdlib only.
"""

from __future__ import annotations


def _browser_content_meaningful(structured: dict) -> bool:
    """Blank-render guard for the ACTIVE finish probe. A page can serve HTTP 200
    with a CLEAN console yet render nothing a user would see — an SPA whose JS
    never mounted, or an empty shell. `_browser_verified` (console-errors only)
    passes that page; this guard catches it.

    Meaningful iff there is real visible content (title + body text >= 20 chars
    combined), a non-empty browser-rendered ``text/plain`` document, rendered
    content-bearing semantics, OR interactive structure. The
    browser daemon reports ``visible_semantic_elements`` after checking geometry,
    viewport intersection, and computed visibility for content-bearing headings.
    This distinguishes a short but valid rendered heading from an empty SPA shell.
    The browser daemon's element walker
    (`_get_elements`) indexes links/forms/buttons/inputs/clickables, so a
    non-empty `elements` list means the page has actionable structure even when
    its text is sparse. (`links`/`forms` count fields are honored too if a daemon
    variant supplies them.)"""
    title = str(structured.get("title", "") or "")
    text = str(structured.get("text", "") or "")
    if len((title + " " + text).strip()) >= 20:
        return True
    # A plain-text HTTP response is itself the served document, not an empty SPA
    # shell.  ``document_content_type`` is captured from browser-owned main-response
    # metadata, and the body text is rendered DOM/accessibility evidence.  Keep
    # the sparse exception exact to text/plain so a short HTML loading placeholder
    # does not bypass the existing blank-render guard.
    raw_content_type = structured.get("document_content_type")
    if (
        isinstance(raw_content_type, str)
        and raw_content_type.split(";", 1)[0].strip().lower() == "text/plain"
        and bool(text.strip())
    ):
        return True
    semantic_count = structured.get("visible_semantic_elements")
    if (
        isinstance(semantic_count, int)
        and not isinstance(semantic_count, bool)
        and semantic_count > 0
    ):
        return True
    elements = structured.get("elements")
    if isinstance(elements, list) and len(elements) > 0:
        return True
    if structured.get("links") or structured.get("forms"):
        return True
    return False
