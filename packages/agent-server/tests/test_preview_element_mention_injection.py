from __future__ import annotations

from disco.agent_server.preview_inject import (
    ELEMENT_MENTION_MARKER,
    inject_element_mention_picker,
)


def test_element_mention_picker_injected_before_body_close_once() -> None:
    html = b"<html><body><h1>Hello</h1></body></html>"
    injected = inject_element_mention_picker(html, "text/html; charset=utf-8")

    assert ELEMENT_MENTION_MARKER.encode() in injected
    assert injected.index(ELEMENT_MENTION_MARKER.encode()) < injected.lower().index(b"</body>")

    reinjected = inject_element_mention_picker(injected, "text/html")
    assert reinjected == injected
    assert reinjected.count(ELEMENT_MENTION_MARKER.encode()) == 1


def test_element_mention_picker_only_injects_text_html() -> None:
    body = b"<html><body>json-ish</body></html>"

    assert inject_element_mention_picker(body, "application/json") == body
    assert inject_element_mention_picker(body, None) == body


def test_element_mention_picker_leaves_non_html_and_oversized_bodies_untouched() -> None:
    css = b"body { color: red; }"
    assert inject_element_mention_picker(css, "text/css") == css

    html = b"<html><body>large</body></html>"
    assert inject_element_mention_picker(html, "text/html", max_bytes=len(html) - 1) == html
