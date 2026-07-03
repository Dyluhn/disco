"""[P10] Export render-correctness check — the real executor for the inert
``ExportContract`` ``validate`` stage.

The build's finish gate must not report FINISHED for a deck/document deliverable
that rendered BLANK, TRUNCATED, or CORRUPT — the "looks done but the file is
empty" false-affordance-of-completeness. The trap today is that the producer
stamps ``slide_count = len(deck.slides)`` (the model's DECLARED count, computed
from the deck *spec* before rendering) into the tool result, and the gate trusts
it. A five-slide spec can still render to empty text boxes or a zero-byte file.

This module parses the ACTUAL output bytes and reports what really rendered:

  * ``valid_header``   — the bytes are a well-formed file of the claimed format
                         (``%PDF`` magic / a PPTX OOXML zip / non-trivial HTML).
  * ``unit_count``     — slides (deck) or pages (pdf) actually present in the bytes.
  * ``visible_text_len`` — length of rendered text with template CHROME removed,
                         so brand wordmarks can't mask a content-empty deck.
  * ``truncated``      — fewer units rendered than the model declared.
  * ``non_blank`` / ``ok`` — the gate's yes/no.

Pure and dependency-light on purpose: stdlib ``zipfile``/``re`` only, no
``python-pptx``/WeasyPrint — those live in ``tools`` which ``core`` may not
import. The producer (``tools``) calls :func:`check_export_render` at render time
and stamps :class:`ExportRenderFacts` into ``tool_result.structured``; the finish
gate (``core``) reads it back via :func:`latest_export_render_facts`.

THREAT MODEL (documented limitation, Codex P10-4/P10-5): the input is the OUTPUT of
a trusted renderer (LibreOffice / python-pptx / Marp / the C3 template), NOT an
adversary. These checks catch RENDERER FAILURES — blank, truncated, zero-unit,
missing-header — not maliciously-crafted bytes that mimic a valid header (e.g. a
non-PDF with ``%PDF``+``%%EOF``+``/Type /Page`` padding, or a zip with a junk
``ppt/media`` entry). A full parse (pypdf/python-pptx) would close that gap but
belongs in ``tools`` and is out of scope for the finish gate's core-side check.
"""

from __future__ import annotations

import io
import re
import zipfile

from pydantic import BaseModel, ConfigDict

from ..events import Event, EventSource, MessageEvent, ObservationEvent

# The structured-payload key the producer stamps and the gate reads. One name,
# both sides — a typo here silently disables the gate, so it is a single const.
EXPORT_RENDER_KEY = "export_render"

# Stable marker embedded in the finish gate's refusal reminder. The gate counts
# its OWN prior refusals from the event log (stateless — no loop attribute), and
# the live proof asserts the gate fired by grepping this token. Public so the gate
# and its tests share ONE literal.
EXPORT_GATE_TOKEN = "EXPORT RENDER CHECK"
# Release valve: after N real refusals the gate finishes anyway with a loud
# UNVERIFIED warning rather than trapping the run behind a broken renderer.
EXPORT_GATE_MAX_REFUSALS = 3

# Below this many characters of chrome-stripped rendered text a TEXT export
# (html/pptx) is treated as content-empty (a blank render). Deliberately LOW: the
# gate should refuse only when confident the export is broken, never bounce a
# terse but real deck. A single real headline clears it; chrome alone does not.
_TEXT_FLOOR = 12

# PDF text lives in compressed content streams (no cheap glyph extraction), so a
# PDF's "non-blank" is a BYTE floor, not a char floor — a real rendered page is
# comfortably multi-KB; a truncated/near-empty PDF falls below this. Kept separate
# from _TEXT_FLOOR because bytes and characters are different units (mixing them
# made a legit 4KB PDF read as blank — the bug this constant fixes).
_PDF_BYTE_FLOOR = 2048

# Template chrome the default deck renderer stamps on EVERY slide (the "Disco."
# wordmark + the Latin colophon). Counting it as content would let a genuinely
# blank deck read as non-blank, so it is removed before measuring text. Matching
# the renderer's literal strings (_pptx_render._brand_marks_html) keeps this
# honest — if the chrome text changes, this list must too (a test pins it).
_CHROME_TOKENS = (
    "Disco.",
    "disco",
    "I learn; I become acquainted with.",
    "discere",
    "to learn",
)

_PDF_PAGE_RE = re.compile(rb"/Type\s*/Page(?![s])")
# Capture the id VALUE, not just the attribute — renderers (the C3 deck template)
# stamp data-slide-id on child elements too, so counting occurrences overcounts;
# counting DISTINCT ids gives the true slide count.
_SLIDE_ID_RE = re.compile(r'data-slide-id\s*=\s*"([^"]*)"')
_SECTION_RE = re.compile(r"<section\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# Strip <script>/<style> BODIES before measuring visible text — their contents are
# not rendered prose (a 2MB inlined stylesheet must not read as slide content).
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
# Strip default-template CHROME by CLASS (the "Disco." wordmark + Latin colophon +
# their bc-* pieces) BEFORE measuring content — token-matching the rendered strings
# is fragile (tag-stripping inserts spaces: `Disco<span>.` → "Disco ."), so a blank
# BRANDED deck slipped past as non-blank. Looped to unwind one nesting level at a
# time (the colophon nests bc-* divs). Structural, so it survives chrome text edits.
_CHROME_CLASS_RE = re.compile(
    r"""<(\w+)[^>]*\bclass=(["'])(?:[^"']*\s)?(?:brand-wordmark|brand-colophon|wordmark|colophon|watermark|bc-[\w-]+)(?:\s[^"']*)?\2[^>]*>.*?</\1>""",
    re.IGNORECASE | re.DOTALL,
)
# Visual content that is NOT text — an image/figure-only slide is a real deck, not
# blank (mirrors the pptx embedded-media rule). Its presence makes a slide non-blank.
# LINEAR by construction — every branch is a single tag with bounded [^>]* / quoted
# attrs, no unbounded backtracking. The svg/canvas branch matches an opening tag
# followed (after optional whitespace) by a CHILD element start `<` that is not the
# closing tag or a comment — i.e. inline svg/canvas WITH content — instead of the
# old `.*?\S.*?</\1>`, which was O(n^2) on a truncated/unclosed large inline SVG and
# stalled the checker on exactly the broken chart decks the gate must refuse. An
# svg/canvas holding only TEXT is still caught via the visible-text floor.
_HTML_MEDIA_RE = re.compile(
    r"""
    <img\b[^>]*\bsrc\s*=\s*(?:"\s*[^"\s][^"]*"|'\s*[^'\s][^']*')[^>]*>
    |<(?:video|iframe|embed)\b[^>]*\bsrc\s*=\s*(?:"\s*[^"\s][^"]*"|'\s*[^'\s][^']*')[^>]*>
    |<object\b[^>]*\bdata\s*=\s*(?:"\s*[^"\s][^"]*"|'\s*[^'\s][^']*')[^>]*>
    |<(?:svg|canvas)\b[^>]*>\s*<(?![/!])
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PPTX_TEXT_RE = re.compile(rb"<a:t>(.*?)</a:t>", re.IGNORECASE | re.DOTALL)
_SLIDE_XML_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")


class ExportRenderFacts(BaseModel):
    """What a deck/document deliverable ACTUALLY rendered to (from its bytes).

    Frozen value object, JSON-round-trippable, stamped into
    ``tool_result.structured[EXPORT_RENDER_KEY]`` by the producer and read back by
    the finish gate. ``ok`` is the single yes/no the gate acts on; the other fields
    exist so a refusal reminder can name the concrete failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fmt: str
    byte_len: int
    valid_header: bool
    unit_count: int  # slides (deck) or pages (pdf) present in the rendered bytes
    declared_units: int | None = None  # the model's claimed slide/page count
    visible_text_len: int = 0
    truncated: bool = False
    non_blank: bool = False
    ok: bool = False
    detail: str = ""


def _strip_chrome(text: str) -> str:
    for tok in _CHROME_TOKENS:
        text = text.replace(tok, " ")
    return re.sub(r"\s+", " ", text).strip()


def _html_facts(html: str) -> tuple[int, int, bool, bool]:
    """(unit_count, visible_text_len, valid_header, non_blank) for an HTML deck/page."""
    slides = len({sid for sid in _SLIDE_ID_RE.findall(html) if sid})
    if slides == 0:
        slides = len(_SECTION_RE.findall(html))
    # Remove <script>/<style> bodies, then unwind template CHROME by class (looped so
    # nested colophon pieces go too), then strip tags + any residual chrome tokens.
    body = _SCRIPT_STYLE_RE.sub(" ", html)
    prev = ""
    while prev != body:
        prev = body
        body = _CHROME_CLASS_RE.sub(" ", body)
    visible = _strip_chrome(_TAG_RE.sub(" ", body))
    has_media = bool(_HTML_MEDIA_RE.search(body))
    valid_header = "<" in html and ">" in html and bool(visible or slides or has_media)
    non_blank = len(visible) >= _TEXT_FLOOR or has_media
    # A single-page (non-deck) HTML doc is one unit if it has real content/media.
    if slides == 0 and non_blank:
        slides = 1
    return slides, len(visible), valid_header, non_blank


def _pptx_facts(data: bytes) -> tuple[int, int, bool, bool]:
    """(slide_count, visible_text_len, valid_header, non_blank) for PPTX bytes.

    ``non_blank`` accepts EITHER extractable slide text OR embedded media: a
    Marp-style image-based deck (each slide a rendered picture, no ``<a:t>`` runs)
    is genuine content, not blank — checking only text would false-refuse it."""
    if not data.startswith(b"PK"):
        return 0, 0, False, False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            slide_names = [n for n in names if _SLIDE_XML_RE.match(n)]
            texts: list[str] = []
            for n in slide_names:
                try:
                    xml = zf.read(n)
                except Exception:
                    continue
                for m in _PPTX_TEXT_RE.findall(xml):
                    texts.append(m.decode("utf-8", "replace"))
    except (zipfile.BadZipFile, OSError):
        return 0, 0, False, False
    visible = _strip_chrome(" ".join(texts))
    # A valid OOXML deck always carries [Content_Types].xml; a zip lacking it and
    # any slide is not a real presentation.
    valid_header = "[Content_Types].xml" in names and len(slide_names) > 0
    has_media = any(n.startswith("ppt/media/") for n in names)
    non_blank = valid_header and (len(visible) >= _TEXT_FLOOR or has_media)
    return len(slide_names), len(visible), valid_header, non_blank


def _pdf_facts(data: bytes) -> tuple[int, int, bool, bool]:
    """(page_count, visible_text_len, valid_header, non_blank) for PDF bytes.

    Page text is not extracted (PDF text lives in compressed content streams that
    need a full parser); ``non_blank`` is a BYTE floor — a real rendered page is
    comfortably multi-KB while a truncated/near-empty PDF falls below it.
    """
    valid_header = data[:5].startswith(b"%PDF") and b"%%EOF" in data[-1024:]
    pages = len(_PDF_PAGE_RE.findall(data))
    # Reported proxy (for the steer message), plus the real byte-floor non_blank.
    text_proxy = 0 if not valid_header else max(0, (len(data) // 1024) - 1)
    non_blank = valid_header and len(data) >= _PDF_BYTE_FLOOR
    return pages, text_proxy, valid_header, non_blank


def check_export_render(
    fmt: str,
    data: bytes | None = None,
    *,
    text: str | None = None,
    declared_units: int | None = None,
    declared_exact: bool = True,
) -> ExportRenderFacts:
    """Parse a rendered export's bytes/text and report what really rendered.

    ``fmt`` is the producer's format id ("html"/"pptx"/"pdf"). Pass ``text`` for
    HTML (the rendered string) or ``data`` for pptx/pdf (the bytes). ``ok`` is the
    finish gate's signal: a well-formed file with at least one rendered unit, real
    (chrome-stripped) content, and no truncation below the declared count.

    ``declared_exact`` says whether ``declared_units`` is authoritative (C1/pptx-
    native decks, where it is ``len(deck.slides)``) or a heuristic (markdown/Marp
    decks, where it comes from a separator split). Exact ⇒ ANY shortfall is
    truncation; heuristic ⇒ tolerate a one-unit split miscount, flag only GROSS loss.
    """
    f = (fmt or "").strip().lower()
    is_pdf = f in ("pdf", "document")
    if f in ("html", "htm", "deck_html"):
        raw = text if text is not None else (data or b"").decode("utf-8", "replace")
        byte_len = len(raw.encode("utf-8"))
        units, visible_len, valid, non_blank = _html_facts(raw)
    elif f in ("pptx", "deck", "deck_pptx"):
        buf = data or b""
        byte_len = len(buf)
        units, visible_len, valid, non_blank = _pptx_facts(buf)
    elif is_pdf:
        buf = data or b""
        byte_len = len(buf)
        units, visible_len, valid, non_blank = _pdf_facts(buf)
    else:
        # Unknown format → honestly unverifiable (never fabricate a pass/fail).
        return ExportRenderFacts(
            fmt=f, byte_len=len(data or b""), valid_header=False, unit_count=0,
            declared_units=declared_units, ok=False,
            detail=f"no render validator for export format {f!r}",
        )

    # Exact declared counts (C1/pptx) → any shortfall is truncation; heuristic
    # markdown counts → tolerate a one-unit split miscount (flag only GROSS loss).
    slack = 0 if declared_exact else 1
    truncated = declared_units is not None and declared_units > 0 and units < declared_units - slack
    ok = valid and units >= 1 and non_blank and not truncated

    if not valid:
        detail = f"{f} bytes are not a well-formed {f} file (invalid/corrupt/truncated)"
    elif units < 1:
        detail = f"{f} rendered zero {'pages' if f in ('pdf', 'document') else 'slides'}"
    elif truncated:
        detail = f"{f} rendered {units} of {declared_units} declared units (truncated)"
    elif not non_blank:
        detail = f"{f} rendered but is content-empty ({visible_len} chars of real text)"
    else:
        detail = f"{f} render ok: {units} unit(s), {visible_len} chars"

    return ExportRenderFacts(
        fmt=f, byte_len=byte_len, valid_header=valid, unit_count=units,
        declared_units=declared_units, visible_text_len=visible_len,
        truncated=truncated, non_blank=non_blank, ok=ok, detail=detail,
    )


def render_facts_from_structured(structured: object) -> ExportRenderFacts | None:
    """Reconstruct :class:`ExportRenderFacts` from a tool result's ``structured``
    payload (the ``export_render`` slot). Returns None when absent/malformed —
    absence is honest "unstamped", never a fabricated verdict."""
    if not isinstance(structured, dict):
        return None
    raw = structured.get(EXPORT_RENDER_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return ExportRenderFacts.model_validate(raw)
    except Exception:
        return None


def latest_export_render_facts(events: list[Event]) -> ExportRenderFacts | None:
    """The most recent stamped :class:`ExportRenderFacts` in the event log, or None.

    Scans newest-first for a successful tool observation whose ``structured``
    carries an ``export_render`` slot — mirroring how the finish gate already
    locates the latest deliverable. None ⇒ no export was produced/stamped (the gate
    then falls through; it never invents a verdict)."""
    for ev in reversed(events):
        if (
            isinstance(ev, ObservationEvent)
            and ev.tool_result.success
            and ev.tool_result.structured
        ):
            facts = render_facts_from_structured(ev.tool_result.structured)
            if facts is not None:
                return facts
    return None


def latest_export_render_index(events: list[Event]) -> int:
    """Index of the newest stamped export observation, or -1. Lets the gate tell
    whether an app deliverable is NEWER than the export (a fresh app handoff that
    supersedes a stale deck) vs OLDER (a stale app that must not mask a fresh broken
    deck) — the difference between falling through and refusing."""
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        if (
            isinstance(ev, ObservationEvent)
            and ev.tool_result.success
            and ev.tool_result.structured
            and render_facts_from_structured(ev.tool_result.structured) is not None
        ):
            return i
    return -1


def count_export_gate_refusals(events: list[Event]) -> int:
    """How many times the export gate has already refused this run — counted from
    its own refusal markers in the log, so the cap needs no loop state and survives
    reconstruction/resume. Restricted to ENVIRONMENT-source messages (the gate emits
    those) so a USER/AGENT message that merely quotes the token can't force an early
    release of the cap."""
    return sum(
        1
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and EXPORT_GATE_TOKEN in (e.message.content or "")
    )


def export_gate_refusal_reminder(facts: ExportRenderFacts) -> str:
    """The <system-reminder> steer for a blank/truncated/corrupt export."""
    remedy = (
        "re-generate the deck/document so every slide/page has real content"
        if facts.non_blank is False
        else "re-generate it so the whole export renders (no truncation/corruption)"
    )
    declared = f", declared {facts.declared_units}" if facts.declared_units else ""
    return (
        "<system-reminder>\n"
        f"{EXPORT_GATE_TOKEN}: the exported {facts.fmt} is not a valid deliverable. "
        f"{facts.detail}. (rendered {facts.unit_count} unit(s){declared}, "
        f"{facts.visible_text_len} chars of content.)\n"
        f"Do NOT finish yet — {remedy}, then finish again.\n"
        "The task is NOT complete while the export is blank/truncated.\n"
        "</system-reminder>"
    )


def export_gate_release_warning(facts: ExportRenderFacts) -> str:
    """The loud UNVERIFIED warning emitted when the refusal cap releases."""
    return (
        f"⚠ Finishing WITHOUT a valid export ({EXPORT_GATE_MAX_REFUSALS} attempts): "
        f"{facts.detail}. The exported file is UNVERIFIED and may be blank/truncated — "
        "say so clearly in your summary."
    )
