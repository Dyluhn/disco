"""HTML injection helpers for preview-only affordances."""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources

ELEMENT_MENTION_MARKER = "<!-- disco-element-mention-picker:v1 -->"
MAX_ELEMENT_MENTION_HTML_BYTES = 2 * 1024 * 1024


@lru_cache(maxsize=1)
def _element_mention_script() -> str:
    return (
        resources.files("disco.agent_server")
        .joinpath("element_mention_picker.js")
        .read_text(encoding="utf-8")
    )


def _is_text_html(content_type: str | None) -> bool:
    if not content_type:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "text/html"


def inject_element_mention_picker(
    body: bytes,
    content_type: str | None,
    *,
    max_bytes: int = MAX_ELEMENT_MENTION_HTML_BYTES,
) -> bytes:
    """Inject the inert element-mention picker into small text/html documents.

    The script itself only installs a message listener until armed by the parent.
    Injection is idempotent via ``ELEMENT_MENTION_MARKER`` and returns ``body``
    unchanged for non-HTML responses, oversized documents, or non-UTF-8 HTML.
    """

    if not _is_text_html(content_type):
        return body
    if len(body) > max_bytes:
        return body
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    if ELEMENT_MENTION_MARKER in html:
        return body

    script_tag = f"{ELEMENT_MENTION_MARKER}\n<script>\n{_element_mention_script()}\n</script>"
    matches = list(re.finditer(r"</body\s*>", html, flags=re.IGNORECASE))
    if matches:
        match = matches[-1]
        html = f"{html[: match.start()]}{script_tag}\n{html[match.start():]}"
    else:
        html = f"{html}\n{script_tag}"
    return html.encode("utf-8")
