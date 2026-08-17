"""A1.1 — data-oid stamper tests."""

from __future__ import annotations

import re

from disco.agent_server.oid_stamp import stamp_oids


def _oids(html: str) -> list[str]:
    return re.findall(r'data-oid="([^"]+)"', html)


def test_stamps_visible_elements_with_relpath_and_line() -> None:
    html = "<html><body>\n<h1>Title</h1>\n<p>Hi <span>there</span></p>\n</body></html>"
    out = stamp_oids(html, "index.html")
    oids = _oids(out)
    # body(1), h1(2), p(3), span(3) — each tagged with the file + its source line.
    assert "index.html:2" in oids  # h1 on line 2
    assert "index.html:3" in oids  # p on line 3
    assert oids.count("index.html:3") == 2  # p + span both on line 3
    assert all(o.startswith("index.html:") for o in oids)


def test_skips_non_visual_tags() -> None:
    html = (
        "<html><head><title>T</title><script>var x=1;</script>"
        "<style>.a{}</style></head><body><h1>Hi</h1></body></html>"
    )
    out = stamp_oids(html, "page.html")
    # head/title/script/style/html never get a data-oid; the h1 does.
    assert (
        "<script" in out
        and "data-oid" not in out.split("</body>")[0].split("<script")[1].split(">")[0]
    )
    assert any(o.startswith("page.html:") for o in _oids(out))
    # The script/style/title tags themselves carry no oid.
    for tag in ("<script", "<style", "<title", "<head"):
        seg = out.split(tag, 1)[1].split(">", 1)[0]
        assert "data-oid" not in seg


def test_idempotent_does_not_reclobber_existing_oid() -> None:
    html = '<html><body><h1 data-oid="orig.html:9">Kept</h1><p>New</p></body></html>'
    out = stamp_oids(html, "index.html")
    # The pre-existing oid is preserved; only the unstamped <p> gets a fresh one.
    assert "orig.html:9" in _oids(out)
    assert any(o.startswith("index.html:") for o in _oids(out))
    # Re-stamping the output is a no-op (same oids).
    assert sorted(_oids(stamp_oids(out, "index.html"))) == sorted(_oids(out))


def test_empty_or_unparseable_returns_input() -> None:
    assert stamp_oids("", "x.html") == ""
    assert stamp_oids("   ", "x.html").strip() == ""


def test_preserves_doctype_and_content() -> None:
    html = "<!DOCTYPE html><html><body><h1>Hello</h1></body></html>"
    out = stamp_oids(html, "index.html")
    assert "<!DOCTYPE html>" in out
    assert "Hello" in out
