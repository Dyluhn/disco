"""B3 — the build agent's `search`/`extract` tools return CLEAN MARKDOWN, not a
Python repr (`str(list_of_dicts)`), so the driver reads results cleanly without
brace/quote noise polluting its context."""

from __future__ import annotations

from disco.tools.builtin.retrieval import (
    _EXTRACT_CHAR_BUDGET,
    _extract_markdown,
    _search_markdown,
)


def test_search_renders_a_ranked_markdown_list():
    results = [
        {"url": "https://a.test/x", "title": "Alpha", "snippet": "first  result\nhere"},
        {"url": "https://b.test/y", "title": "Beta", "snippet": ""},
    ]
    md = _search_markdown("widgets", results)
    assert 'Search results for "widgets":' in md
    assert "1. [Alpha](https://a.test/x) — first result here" in md  # whitespace normalized
    assert "2. [Beta](https://b.test/y)" in md
    assert "{" not in md and "'" not in md  # no python-repr noise


def test_search_handles_empty():
    assert _search_markdown("nothing", []) == 'No results for "nothing".'


def test_extract_renders_title_url_and_body():
    doc = {"url": "https://x.test", "title": "A Page", "content": "Hello world.", "status": "ok"}
    md, ok, err = _extract_markdown(doc)
    assert ok and err is None
    assert md.startswith("# A Page\n<https://x.test>\n\nHello world.")


def test_extract_budgets_long_content():
    doc = {"url": "u", "title": "T", "content": "x" * (_EXTRACT_CHAR_BUDGET + 5000), "status": "ok"}
    md, ok, _ = _extract_markdown(doc)
    assert ok and "truncated" in md and len(md) < _EXTRACT_CHAR_BUDGET + 500


def test_extract_surfaces_failure():
    doc = {"url": "u", "title": "", "content": "", "fetched_ok": False, "status": "blocked"}
    md, ok, err = _extract_markdown(doc)
    assert not ok and err == "blocked" and "Could not extract" in md


def test_extract_accepts_a_bare_string():
    md, ok, _ = _extract_markdown("just the content")
    assert ok and md == "just the content"
