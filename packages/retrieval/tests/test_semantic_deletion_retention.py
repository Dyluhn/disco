"""Bundled extraction must preserve source retractions as deleted text."""

from unittest.mock import AsyncMock

import httpx
import pytest
from disco.retrieval import bundled_providers


@pytest.mark.parametrize("tag", ["s", "del", "strike"])
async def test_semantic_deleted_guidance_stays_distinct_from_current_text(monkeypatch, tag):
    old = "Old withdrawn guidance is obsolete."
    current = "The current guidance supersedes the withdrawn recommendation."
    html = (
        "<html><title>Versioned guidance</title><article><h1>Guide</h1><p>"
        + ("Context for the versioned guidance. " * 12)
        + f"<{tag}>{old}</{tag}> {current}</p></article></html>"
    )
    fetch = AsyncMock(
        return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
    )
    monkeypatch.setattr(bundled_providers, "guarded_get", fetch)
    doc = await bundled_providers.LocalExtractionProvider().extract("https://example.test/guidance")
    assert doc.fetched_ok and doc.status == "ok"
    assert old in doc.content and current in doc.content
    assert f"~~{old}~~" in doc.content
    assert any(f"~~{old}~~" in p.text for p in doc.passages)
    assert doc.url == "https://example.test/guidance"
    fetch.assert_awaited_once()


async def test_ordinary_guidance_and_link_remain_unchanged(monkeypatch):
    text = "Current guidance with an ordinary source link."
    html = (
        "<html><title>Current guide</title><article><p>"
        + ("Context for the ordinary current guidance. " * 12)
        + text
        + ' <a href="/details">Details</a></p></article></html>'
    )
    monkeypatch.setattr(
        bundled_providers,
        "guarded_get",
        AsyncMock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        ),
    )
    doc = await bundled_providers.LocalExtractionProvider().extract("https://example.test/current")
    assert doc.fetched_ok and text in doc.content
    assert "[Details](/details)" in doc.content
    assert "~~" not in doc.content


async def test_nested_deletion_emits_single_strikethrough(monkeypatch):
    outer_a = "Outer withdrawn guidance"
    inner = "inner withdrawn detail"
    outer_b = "remains withdrawn"
    current = "The current guidance supersedes it."
    html = (
        "<html><title>Versioned guidance</title><article><p>"
        + ("Context for the versioned guidance. " * 12)
        + f"<del>{outer_a} <s>{inner}</s> {outer_b}</del> {current}</p></article></html>"
    )
    monkeypatch.setattr(
        bundled_providers,
        "guarded_get",
        AsyncMock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        ),
    )
    doc = await bundled_providers.LocalExtractionProvider().extract("https://example.test/nested")
    assert doc.fetched_ok and doc.status == "ok"
    assert outer_a in doc.content and inner in doc.content and current in doc.content
    assert "~~~~" not in doc.content
    assert doc.content.count("~~") == 2
    assert any("~~" in p.text and inner in p.text for p in doc.passages)


async def test_link_inside_deletion_preserved(monkeypatch):
    old = "Old withdrawn guidance"
    current = "Current replacement guidance."
    html = (
        "<html><title>Versioned guidance</title><article><p>"
        + ("Context for the versioned guidance. " * 12)
        + f'<s>{old} <a href="/details">Details</a></s> {current}</p></article></html>'
    )
    monkeypatch.setattr(
        bundled_providers,
        "guarded_get",
        AsyncMock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        ),
    )
    doc = await bundled_providers.LocalExtractionProvider().extract("https://example.test/link-del")
    assert doc.fetched_ok and doc.status == "ok"
    assert old in doc.content and current in doc.content
    assert "[Details](/details)" in doc.content
    assert "~~" in doc.content
    assert "~~~~" not in doc.content


async def test_deletion_inside_skipped_noise_leaves_no_markers(monkeypatch):
    text = "Ordinary current guidance stays visible."
    html = (
        "<html><title>Current guide</title>"
        "<nav><s>Nav withdrawn noise</s></nav>"
        "<script><del>var hidden = 1;</del></script>"
        "<article><p>"
        + ("Context for the ordinary current guidance. " * 12)
        + text
        + "</p></article></html>"
    )
    monkeypatch.setattr(
        bundled_providers,
        "guarded_get",
        AsyncMock(
            return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"})
        ),
    )
    doc = await bundled_providers.LocalExtractionProvider().extract("https://example.test/noise")
    assert doc.fetched_ok and doc.status == "ok"
    assert text in doc.content
    assert "~~" not in doc.content
    assert "Nav withdrawn noise" not in doc.content


async def test_deleted_block_keeps_each_citable_passage_marked(monkeypatch):
    old = ("Withdrawn guidance has been replaced. " * 40).strip()
    current = ("Current guidance is authoritative. " * 40).strip()
    html = (
        "<html><title>Withdrawn block</title><article><del>"
        "<h2>Old recommendation</h2><p>" + old + "</p></del>"
        "<h2>Current recommendation</h2><p>" + current + "</p></article></html>"
    )
    monkeypatch.setattr(
        bundled_providers,
        "guarded_get",
        AsyncMock(return_value=httpx.Response(200, text=html)),
    )
    doc = await bundled_providers.LocalExtractionProvider().extract("https://example.test/block")
    assert doc.fetched_ok and doc.status == "ok"
    withdrawn = [p for p in doc.passages if old in p.text]
    assert withdrawn and all(f"~~{old}~~" in p.text for p in withdrawn)
    assert any(current in p.text and "~~" not in p.text for p in doc.passages)


async def test_empty_deleted_content_does_not_add_visible_markers(monkeypatch):
    current = "Current guidance remains authoritative. " * 14
    html = (
        "<html><title>Empty deletion</title><article><p><del></del>"
        + current
        + "</p></article></html>"
    )
    monkeypatch.setattr(
        bundled_providers,
        "guarded_get",
        AsyncMock(return_value=httpx.Response(200, text=html)),
    )
    doc = await bundled_providers.LocalExtractionProvider().extract("https://example.test/empty")
    assert doc.fetched_ok and current.strip() in doc.content
    assert "~~" not in doc.content


async def test_deleted_text_blank_lines_keep_each_passage_marked(monkeypatch):
    first = ("Earlier withdrawn guidance is obsolete. " * 40).strip()
    second = ("Later withdrawn guidance is also obsolete. " * 40).strip()
    current = ("Current replacement guidance applies. " * 40).strip()
    html = (
        "<html><title>Deleted source whitespace</title><article><p><del>"
        + first
        + "\n\n"
        + second
        + "</del></p><h2>Current recommendation</h2><p>"
        + current
        + "</p></article></html>"
    )
    monkeypatch.setattr(
        bundled_providers,
        "guarded_get",
        AsyncMock(return_value=httpx.Response(200, text=html)),
    )
    doc = await bundled_providers.LocalExtractionProvider().extract(
        "https://example.test/whitespace"
    )
    assert doc.fetched_ok
    for old in (first, second):
        passages = [p for p in doc.passages if old in p.text]
        assert passages and all(f"~~{old}~~" in p.text for p in passages)
    assert any(current in p.text and "~~" not in p.text for p in doc.passages)


async def test_literal_newline_escapes_remain_one_deleted_span(monkeypatch):
    old = ("Withdrawn text can contain a literal escape sequence. " * 12).strip()
    literal = old + chr(92) + "n" + chr(92) + "n" + old
    current = "Current replacement guidance remains active. " * 12
    html = (
        "<html><title>Literal escape</title><article><p><del>"
        + literal
        + "</del> "
        + current
        + "</p></article></html>"
    )
    monkeypatch.setattr(
        bundled_providers,
        "guarded_get",
        AsyncMock(return_value=httpx.Response(200, text=html)),
    )
    doc = await bundled_providers.LocalExtractionProvider().extract("https://example.test/literal")
    assert doc.fetched_ok
    assert f"~~{literal}~~" in doc.content
    assert current.strip() in doc.content
