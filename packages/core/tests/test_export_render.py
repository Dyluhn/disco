"""[P10] Unit tests for the export render-correctness checker.

Bytes here are synthesized to mirror the REAL output layouts (OOXML
ppt/slides/slideN.xml + <a:t> runs, %PDF…%%EOF + /Type /Page, data-slide-id
sections) so the parser is exercised honestly without importing the `tools`
renderers. The end-to-end proof against the actual renderer is the live harness.
"""

from __future__ import annotations

import io
import zipfile

from disco.core.brand.mark import BRAND_CHROME_TEXTS, RENDER_PLACEHOLDERS
from disco.core.contract.export_render import (
    EXPORT_GATE_TOKEN,
    EXPORT_RENDER_KEY,
    ExportRenderFacts,
    check_export_render,
    count_export_gate_refusals,
    export_gate_refusal_reminder,
    export_gate_release_warning,
    latest_export_render_facts,
    render_facts_from_structured,
)
from disco.core.events import (
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    ToolResult,
)

# ── fixtures ────────────────────────────────────────────────────────────────

_WORDMARK = "Disco."  # template chrome the renderer stamps on every slide


def _html_deck(n_slides: int, *, body: str = "Real headline content here") -> str:
    sections = "".join(
        f'<section class="slide" id="slide-{i}" data-slide-id="slide-{i}" '
        f'data-layout="content"><div class="brand-wordmark">{_WORDMARK}</div>'
        f"<h1>{body} {i}</h1></section>"
        for i in range(n_slides)
    )
    return f"<html><body>{sections}</body></html>"


def _html_deck_blank(n_slides: int) -> str:
    # structurally valid sections but only chrome text, no content
    sections = "".join(
        f'<section class="slide" data-slide-id="slide-{i}">'
        f'<div class="brand-wordmark">{_WORDMARK}</div></section>'
        for i in range(n_slides)
    )
    return f"<html><body>{sections}</body></html>"


def _pptx_bytes(n_slides: int, *, text_per_slide: str = "Quarterly revenue up") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for i in range(1, n_slides + 1):
            body = f"<a:t>{text_per_slide} {i}</a:t>" if text_per_slide else ""
            zf.writestr(f"ppt/slides/slide{i}.xml", f"<p:sld><a:p>{body}</a:p></p:sld>")
    return buf.getvalue()


def _pptx_bytes_with_runs(text_runs: tuple[str, ...]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        body = "".join(f"<a:t>{run}</a:t>" for run in text_runs)
        zf.writestr("ppt/slides/slide1.xml", f"<p:sld><a:p>{body}</a:p></p:sld>")
    return buf.getvalue()


def _pptx_image_deck(n_slides: int) -> bytes:
    """A Marp-style image-based deck: slides with NO <a:t> text but embedded media."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for i in range(1, n_slides + 1):
            zf.writestr(f"ppt/slides/slide{i}.xml", "<p:sld><p:pic/></p:sld>")
            zf.writestr(f"ppt/media/image{i}.png", b"\x89PNG\r\n" + b"\x00" * 200)
    return buf.getvalue()


def _pdf_bytes(n_pages: int, *, pad: int = 4096) -> bytes:
    pages = b"".join(b"<< /Type /Page >>\n" for _ in range(n_pages))
    filler = b"stream data " * (pad // 12)
    return b"%PDF-1.7\n" + pages + filler + b"\ntrailer\n%%EOF\n"


# ── HTML ────────────────────────────────────────────────────────────────────


def test_html_good_deck_ok() -> None:
    f = check_export_render("html", text=_html_deck(5), declared_units=5)
    assert f.valid_header and f.unit_count == 5 and f.non_blank and not f.truncated
    assert f.ok is True


def test_html_blank_deck_refused() -> None:
    # sections exist but only chrome text → content-empty → not ok
    f = check_export_render("html", text=_html_deck_blank(4), declared_units=4)
    assert f.unit_count == 4 and f.valid_header
    assert f.non_blank is False and f.ok is False
    assert "content-empty" in f.detail


def test_html_empty_string_refused() -> None:
    f = check_export_render("html", text="", declared_units=3)
    assert f.unit_count == 0 and f.ok is False


def test_html_truncated_refused() -> None:
    # declared 6 slides, only 2 rendered
    f = check_export_render("html", text=_html_deck(2), declared_units=6)
    assert f.unit_count == 2 and f.truncated is True and f.ok is False
    assert "truncated" in f.detail


def test_html_single_page_non_deck_ok() -> None:
    page = "<html><body><h1>Landing page with real content</h1><p>Body copy.</p></body></html>"
    f = check_export_render("html", text=page)
    assert f.unit_count == 1 and f.non_blank and f.ok


# ── PPTX ────────────────────────────────────────────────────────────────────


def test_pptx_good_ok() -> None:
    f = check_export_render("pptx", _pptx_bytes(3), declared_units=3)
    assert f.valid_header and f.unit_count == 3 and f.non_blank and f.ok


def test_pptx_blank_text_refused() -> None:
    f = check_export_render("pptx", _pptx_bytes(3, text_per_slide=""), declared_units=3)
    assert f.valid_header and f.unit_count == 3
    assert f.non_blank is False and f.ok is False


def test_pptx_renderer_brand_chrome_only_refused() -> None:
    assert BRAND_CHROME_TEXTS == (
        "Disco",
        "disco",
        "Latin · verb",
        "/ˈdɪs.koː/",
        "I learn; I become acquainted with.",
        "from discere — to learn",
    )
    f = check_export_render(
        "pptx",
        _pptx_bytes_with_runs(
            (
                "Disco",
                "disco   ",
                "LATIN · VERB    /ˈdɪs.koː/",
                "“I learn; I become acquainted with.”",
                "from discere — to learn",
            )
        ),
        declared_units=1,
    )
    assert f.non_blank is False and f.ok is False


def test_render_placeholders_are_pinned_and_stripped() -> None:
    # RENDER_PLACEHOLDERS must match the renderer literals; a deck of only these is BLANK.
    assert RENDER_PLACEHOLDERS == ("[image]", "(no table data)", "[no data]")
    deck = (
        "<html><body>"
        '<section data-slide-id="s1"><div>[image]</div></section>'
        '<section data-slide-id="s2"><p>(no table data)</p></section>'
        '<section data-slide-id="s3"><div>[no data]</div></section>'
        "</body></html>"
    )
    f = check_export_render("html", text=deck, declared_units=3)
    assert f.unit_count == 3 and f.non_blank is False and f.ok is False


def test_chrome_token_does_not_strip_real_words() -> None:
    # Regression: bare "disco" must NOT gut "discovery"/"disconnect" (word-boundary match).
    deck = (
        '<html><body><section data-slide-id="s"><h1>Discovery Plan for Q3</h1>'
        "</section></body></html>"
    )
    f = check_export_render("html", text=deck, declared_units=1)
    assert f.non_blank is True and f.ok is True  # a real "Discovery Plan" deck is content
    # but the standalone wordmark/headword still strips
    f2 = check_export_render(
        "html",
        text=(
            '<html><body><section data-slide-id="s"><div class="brand-wordmark">'
            "Disco</div><span>disco</span></section></body></html>"
        ),
        declared_units=1,
    )
    assert f2.non_blank is False


def test_length_changing_lowercase_in_style_does_not_drop_content() -> None:
    # Regression: "İ" (U+0130) lowercases to TWO code points; the script/style strip
    # must not index a lowercased copy against the original or it slices out real text.
    html = (
        "<html><style>" + ("İ" * 50) + "</style><body>"
        '<section data-slide-id="s"><h1>Real headline content here</h1></section>'
        "</body></html>"
    )
    f = check_export_render("html", text=html, declared_units=1)
    assert f.non_blank is True and f.ok is True  # real headline survives the strip


def test_head_title_not_counted_as_slide_content() -> None:
    # A blank branded slide whose only text is the doc <title> must still be BLANK.
    html = (
        "<html><head><title>Quarterly Strategy Review Deck</title></head><body>"
        '<section data-slide-id="s"><div class="brand-wordmark">Disco.</div></section>'
        "</body></html>"
    )
    f = check_export_render("html", text=html, declared_units=1)
    assert f.non_blank is False and f.ok is False


def test_pptx_zero_slides_refused() -> None:
    f = check_export_render("pptx", _pptx_bytes(0), declared_units=2)
    assert f.unit_count == 0 and f.valid_header is False and f.ok is False


def test_pptx_not_a_zip_refused() -> None:
    f = check_export_render("pptx", b"not a zip file at all", declared_units=2)
    assert f.valid_header is False and f.unit_count == 0 and f.ok is False
    assert "not a well-formed" in f.detail


def test_pptx_truncated_refused() -> None:
    f = check_export_render("pptx", _pptx_bytes(2), declared_units=8)
    assert f.unit_count == 2 and f.truncated is True and f.ok is False


def test_pptx_image_based_deck_is_non_blank() -> None:
    # Marp image decks have no <a:t> text but real embedded media → NOT blank.
    f = check_export_render("pptx", _pptx_image_deck(4), declared_units=4)
    assert f.valid_header and f.unit_count == 4 and f.non_blank is True and f.ok is True


def test_html_from_bytes_read_back() -> None:
    # the producer read-back path passes bytes (not text); html must still parse.
    f = check_export_render("html", _html_deck(3).encode("utf-8"), declared_units=3)
    assert f.unit_count == 3 and f.non_blank and f.ok


# ── Codex hardening: false-pass / false-refusal edges ───────────────────────

_C3_CHROME = (
    '<div class="brand-wordmark">Disco<span class="dot">.</span></div>'
    '<div class="brand-colophon"><span class="bc-hw">disco</span>'
    '<div class="bc-gloss">“I learn; I become acquainted with.”</div>'
    '<div class="bc-root">from <em>discere</em> — to learn</div></div>'
)


def test_blank_branded_deck_refused() -> None:
    # P10-3: a slide with ONLY the C3 brand chrome (wordmark + colophon) is BLANK —
    # chrome must be stripped structurally so it can't read as content.
    html = f'<html><body><section data-slide-id="slide-0">{_C3_CHROME}</section></body></html>'
    f = check_export_render("html", text=html, declared_units=1)
    assert f.visible_text_len == 0 and f.non_blank is False and f.ok is False


def test_branded_deck_with_real_content_passes() -> None:
    # the chrome strip must NOT eat authored content around it (no new false refusal).
    html = (
        f'<html><body><section data-slide-id="slide-0">{_C3_CHROME}'
        "<h1>Green tea boosts metabolism and focus</h1>"
        "<p>Multiple studies show a measurable effect.</p></section></body></html>"
    )
    f = check_export_render("html", text=html, declared_units=1)
    assert f.non_blank and f.ok and f.visible_text_len >= 12


def test_authored_brand_case_study_class_is_not_chrome() -> None:
    html = (
        '<html><body><section data-slide-id="s">'
        '<div class="brand-case-study"><h2>Acme Corp cut costs 40%</h2>'
        "<p>Real narrative text here.</p></div></section></body></html>"
    )
    f = check_export_render("html", text=html, declared_units=1)
    assert f.ok is True


def test_image_only_html_deck_non_blank() -> None:
    # P10-6: real visual media is content, but empty wrappers/placeholders are not.
    for media in ('<img src="d.png" alt="">', "<svg><rect/></svg>"):
        html = f'<html><body><section data-slide-id="slide-0">{media}</section></body></html>'
        f = check_export_render("html", text=html, declared_units=1)
        assert f.non_blank is True and f.ok is True, media


def test_empty_html_media_placeholders_are_blank() -> None:
    for media in ('<img alt="">', "<figure></figure>"):
        html = f'<html><body><section data-slide-id="slide-0">{media}</section></body></html>'
        f = check_export_render("html", text=html, declared_units=1)
        assert f.non_blank is False and f.ok is False, media


def test_script_img_text_does_not_count_as_media() -> None:
    html = (
        '<html><body><section data-slide-id="slide-0">'
        '<script>const x="<img src=x>"</script></section></body></html>'
    )
    f = check_export_render("html", text=html, declared_units=1)
    assert f.non_blank is False and f.ok is False


def test_truncation_heuristic_tolerates_one_gap_but_exact_does_not() -> None:
    # heuristic (markdown/Marp) declared count → tolerate a one-unit split miscount
    f = check_export_render("html", text=_html_deck(2), declared_units=3, declared_exact=False)
    assert f.unit_count == 2 and f.truncated is False and f.ok is True
    # exact (C1/pptx) declared count → a one-unit shortfall IS truncation (default strict)
    f2 = check_export_render("html", text=_html_deck(1), declared_units=2)
    assert f2.unit_count == 1 and f2.truncated is True and f2.ok is False
    # gross loss is truncation regardless of exactness
    f3 = check_export_render("html", text=_html_deck(2), declared_units=6, declared_exact=False)
    assert f3.truncated is True


def test_object_data_media_counts_as_content() -> None:
    # <object data="..."> uses data=, not src= — a real embedded visual, not blank.
    html = (
        '<html><body><section data-slide-id="s"><object data="chart.svg"></object>'
        "</section></body></html>"
    )
    f = check_export_render("html", text=html, declared_units=1)
    assert f.non_blank is True and f.ok is True


def test_media_regex_is_linear_on_truncated_svg() -> None:
    """A truncated/unclosed large inline SVG (a broken chart deck — exactly what the
    gate must refuse) must NOT stall the checker via catastrophic backtracking. Guard
    with a hard timeout so a reintroduced O(n^2) pattern fails loudly, not hangs CI."""
    import signal

    if not hasattr(signal, "SIGALRM"):  # pragma: no cover - non-Unix
        import pytest

        pytest.skip("SIGALRM unavailable")
    truncated = (
        '<html><body><section data-slide-id="s"><svg viewBox="0 0 9 9"><path d="M0 0"/>'
        + ("x" * 2_000_000)  # no closing </svg>
    )

    def _boom(*_):
        raise TimeoutError("media regex stalled — catastrophic backtracking regressed")

    signal.signal(signal.SIGALRM, _boom)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        f = check_export_render("html", text=truncated, declared_units=1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    assert f.unit_count == 1  # completed without stalling


def test_chrome_strip_is_bounded_on_many_unclosed_opens() -> None:
    """A truncated multi-slide BRANDED deck has many unclosed chrome opens; the
    chrome-strip's inner scan is bounded so it can't go O(opens*n) and stall."""
    import signal

    if not hasattr(signal, "SIGALRM"):  # pragma: no cover - non-Unix
        import pytest

        pytest.skip("SIGALRM unavailable")
    html = "<html><body>" + "".join(
        f'<div class="bc-x">{"a" * 5000}'
        for _ in range(400)  # 400 UNCLOSED chrome opens
    )

    def _boom(*_):
        raise TimeoutError("chrome strip stalled — superlinear scan regressed")

    signal.signal(signal.SIGALRM, _boom)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        check_export_render("html", text=html, declared_units=1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_multi_mb_inline_script_is_fully_stripped() -> None:
    """A real deck ships a ~2.5MB inline bundle in ONE <script>; it MUST be stripped
    from the visible-text measure (else its body reads as content and a blank deck
    passes). This is the correctness cliff a small inner-scan cap would fall off, so
    the strip is a linear str.find walk with NO body-size cap."""
    big_body = "var x=1;const y=2;" * 160_000  # ~2.9 MB of non-prose JS
    html = f"<html><body><h1>Real Slide</h1><script>{big_body}</script></body></html>"
    f = check_export_render("html", text=html, declared_units=1)
    # only "Real Slide" survives as visible text — the 2.9MB body is gone
    assert f.visible_text_len < 50, f.visible_text_len
    assert f.non_blank is False  # a one-heading branded-less deck under the floor


def test_script_style_strip_is_linear_on_many_unclosed() -> None:
    """Many unclosed <style> opens must not stall — the strip is O(n), not the
    O(n^2) a `re.sub(r'<style>.*?</style>')` gives (measured 206x for 16x input)."""
    import signal

    if not hasattr(signal, "SIGALRM"):  # pragma: no cover - non-Unix
        import pytest

        pytest.skip("SIGALRM unavailable")
    html = "<html><body>" + ("<style>" + "x" * 40) * 20_000  # 20k unclosed styles

    def _boom(*_):
        raise TimeoutError("script/style strip stalled — O(n^2) regressed")

    signal.signal(signal.SIGALRM, _boom)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        check_export_render("html", text=html, declared_units=1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_count_refusals_only_environment_source() -> None:
    # P10-2: a USER/AGENT message quoting the token must not advance the cap.
    def m(src: EventSource) -> MessageEvent:
        return MessageEvent(
            source=src, message=LLMMessage(role="user", content=f"{EXPORT_GATE_TOKEN} x")
        )

    events = [m(EventSource.USER), m(EventSource.AGENT), m(EventSource.ENVIRONMENT)]
    assert count_export_gate_refusals(events) == 1


# ── PDF ─────────────────────────────────────────────────────────────────────


def test_pdf_good_ok() -> None:
    f = check_export_render("pdf", _pdf_bytes(4), declared_units=4)
    assert f.valid_header and f.unit_count == 4 and f.non_blank and f.ok


def test_pdf_source_text_chrome_only_refused() -> None:
    f = check_export_render(
        "pdf",
        _pdf_bytes(1),
        text="Disco LATIN · VERB /ˈdɪs.koː/ from discere — to learn",
        declared_units=1,
    )
    assert f.non_blank is False and f.ok is False


def test_pdf_source_text_real_content_passes() -> None:
    f = check_export_render(
        "pdf",
        _pdf_bytes(1),
        text="Quarterly revenue grew 40% across all regions and product lines",
        declared_units=1,
    )
    assert f.non_blank is True and f.ok is True


def test_pdf_without_source_text_keeps_byte_floor_fallback() -> None:
    f = check_export_render("pdf", _pdf_bytes(1), declared_units=1)
    assert f.non_blank is True


def test_pdf_no_header_refused() -> None:
    f = check_export_render("pdf", b"just some text, not a pdf", declared_units=1)
    assert f.valid_header is False and f.ok is False


def test_pdf_truncated_no_eof_refused() -> None:
    # has %PDF + pages but no %%EOF trailer → truncated file
    body = b"%PDF-1.7\n" + b"<< /Type /Page >>\n" * 3 + b"stream " * 400
    f = check_export_render("pdf", body, declared_units=3)
    assert f.valid_header is False and f.ok is False


def test_pdf_near_empty_blank() -> None:
    # valid header+eof but tiny → blank proxy trips
    tiny = b"%PDF-1.7\n<< /Type /Page >>\n%%EOF\n"
    f = check_export_render("pdf", tiny, declared_units=1)
    assert f.non_blank is False and f.ok is False


# ── unknown / round-trip / event reader ─────────────────────────────────────


def test_unknown_format_unverifiable() -> None:
    f = check_export_render("xlsx", b"PK\x03\x04stuff")
    assert f.ok is False and "no render validator" in f.detail


def test_facts_json_round_trip() -> None:
    f = check_export_render("pptx", _pptx_bytes(2), declared_units=2)
    dumped = f.model_dump(mode="json")
    back = ExportRenderFacts.model_validate(dumped)
    assert back == f


def test_render_facts_from_structured() -> None:
    f = check_export_render("html", text=_html_deck(3), declared_units=3)
    structured = {"filename": "deck.html", EXPORT_RENDER_KEY: f.model_dump(mode="json")}
    assert render_facts_from_structured(structured) == f
    assert render_facts_from_structured({"filename": "x"}) is None
    assert render_facts_from_structured("nope") is None
    assert render_facts_from_structured({EXPORT_RENDER_KEY: "bad"}) is None


def _obs(tool_name: str, structured: dict | None, *, success: bool = True) -> ObservationEvent:
    return ObservationEvent(
        action_id="a1",
        tool_result=ToolResult(
            call_id="c1", tool_name=tool_name, success=success, content="", structured=structured
        ),
    )


def test_latest_export_render_facts_picks_newest() -> None:
    good = check_export_render("html", text=_html_deck(3), declared_units=3)
    blank = check_export_render("html", text=_html_deck_blank(3), declared_units=3)
    events = [
        MessageEvent(source=EventSource.AGENT, message=LLMMessage(role="assistant", content="hi")),
        _obs("slides_generate", {"filename": "a", EXPORT_RENDER_KEY: good.model_dump(mode="json")}),
        _obs("file_write", {"path": "notes.txt"}),  # no export_render → skipped
        _obs(
            "slides_generate", {"filename": "b", EXPORT_RENDER_KEY: blank.model_dump(mode="json")}
        ),
    ]
    facts = latest_export_render_facts(events)
    assert facts == blank  # newest stamped wins


def test_latest_export_render_facts_none_when_absent() -> None:
    events = [_obs("file_write", {"path": "x.txt"}), _obs("shell", None)]
    assert latest_export_render_facts(events) is None


def test_latest_export_render_facts_skips_failed_obs() -> None:
    good = check_export_render("pptx", _pptx_bytes(2), declared_units=2)
    events = [
        _obs("slides_generate", {EXPORT_RENDER_KEY: good.model_dump(mode="json")}, success=False),
    ]
    assert latest_export_render_facts(events) is None


# ── gate helpers (pure) ─────────────────────────────────────────────────────


def _refusal_msg() -> MessageEvent:
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(
            role="user", content=f"<system-reminder>\n{EXPORT_GATE_TOKEN}: x\n</system-reminder>"
        ),
    )


def test_count_export_gate_refusals() -> None:
    events = [_refusal_msg(), _obs("file_write", {"path": "x"}), _refusal_msg()]
    assert count_export_gate_refusals(events) == 2
    assert count_export_gate_refusals([]) == 0


def test_refusal_reminder_blank_vs_truncated() -> None:
    blank = check_export_render("html", text=_html_deck_blank(3), declared_units=3)
    msg = export_gate_refusal_reminder(blank)
    assert EXPORT_GATE_TOKEN in msg and "real content" in msg and "NOT complete" in msg

    trunc = check_export_render("html", text=_html_deck(2), declared_units=9)
    tmsg = export_gate_refusal_reminder(trunc)
    assert "truncation/corruption" in tmsg and "declared 9" in tmsg


def test_release_warning() -> None:
    blank = check_export_render("pptx", _pptx_bytes(2, text_per_slide=""), declared_units=2)
    w = export_gate_release_warning(blank)
    assert "UNVERIFIED" in w and "3 attempts" in w
