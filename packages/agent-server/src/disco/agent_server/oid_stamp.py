"""A1 — origin-id (data-oid) stamping for click-to-edit.

Stamps each visible element of a built HTML page with ``data-oid="{relpath}:{line}"``
where ``line`` is the element's source line in the workspace HTML file. The in-frame
selection agent (frontend selectionAgent.ts ``resolveRef``) already parses that exact
attribute into a ``SourceRef{file, line}``, so a click in the preview maps back to the
source line and an edit becomes a steer instruction the agent can act on.

Why server-side: the source line is only available from a line-aware parser. The
browser's DOMParser discards source-line info, so client-side stamping can't produce
``file:line``. lxml's ``element.sourceline`` is exact (verified), so we stamp here.

Scope v1: STATIC HTML (the common build deliverable). React/JSX live apps need build-
time JSX instrumentation (a babel/SWC plugin) to map rendered DOM → source — that is a
documented follow-up, NOT handled here.
"""

from __future__ import annotations

# Elements that carry no editable content / aren't meaningful click targets.
_SKIP_TAGS = frozenset(
    {"html", "head", "meta", "link", "title", "base", "script", "style", "br", "hr"}
)


def stamp_oids(html: str, relpath: str) -> str:
    """Return ``html`` with ``data-oid="{relpath}:{sourceline}"`` added to every visible
    element that doesn't already carry one.

    Idempotent: an element with an existing ``data-oid`` is left untouched (re-stamping a
    page is a no-op). Skips non-visual / non-editable tags. On any parse failure the
    original HTML is returned unchanged (never break the preview).
    """
    if not html.strip():
        return html
    try:
        from lxml import html as lhtml
    except Exception:  # noqa: BLE001 — lxml absent → serve unstamped, never crash
        return html

    try:
        doc = lhtml.document_fromstring(html)
    except Exception:  # noqa: BLE001 — unparseable → return as-is
        return html

    stamped = False
    for el in doc.iter():
        tag = el.tag
        if not isinstance(tag, str):  # comments / PIs have non-str tags
            continue
        if tag.lower() in _SKIP_TAGS:
            continue
        if el.get("data-oid") is not None:
            continue
        line = el.sourceline or 0
        el.set("data-oid", f"{relpath}:{line}")
        stamped = True

    if not stamped:
        return html
    # Re-serialize as a full document, preserving the doctype. encoding="unicode"
    # yields str at runtime; coerce for the typechecker (tostring is typed str|bytes).
    out = lhtml.tostring(
        doc, encoding="unicode", method="html", doctype="<!DOCTYPE html>"
    )
    return out if isinstance(out, str) else out.decode("utf-8")
