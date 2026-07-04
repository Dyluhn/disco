"""Medium hints for visual artifact verification.

The host verifier receives bounded evidence, not the builder transcript. These
helpers keep medium detection deterministic and cheap so prompt guidance can
judge slide decks and mobile/PWA surfaces by the right rules.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Literal

from pydantic import BaseModel, ConfigDict


class VerifierMediumHint(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["web", "deck", "mobile"] = "web"
    reason: str = ""
    viewport_width: int | None = None
    viewport_height: int | None = None
    review_guidance: str = ""


DECK_REVIEW_GUIDANCE = (
    "Medium: slide deck. Judge slide composition, not webpage layout. Bottom "
    "whitespace is correct when it gives the slide a presentation-safe baseline. "
    "Use a 24px visible type floor, judge at presentation distance, and expect one "
    "clear takeaway per slide."
)

MOBILE_REVIEW_GUIDANCE = (
    "Medium: mobile/PWA. Review at a 390x844 viewport with touch interaction rules. "
    "Interactive targets should be at least 44px, controls need visible :active or "
    "pressed feedback, and content should respect safe areas without clipped fixed "
    "bars or unreachable controls."
)

_MAX_SCALE_ONE_RE = re.compile(r"(?:^|[,;\s])maximum-scale\s*=\s*1(?:\.0+)?(?:$|[,;\s])")


class _MediumHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.deck_markers = 0
        self.viewport_content = ""
        self.manifest_hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        tag_l = tag.lower()
        if tag_l == "section":
            classes = set(attr.get("class", "").lower().split())
            if "slide" in classes or "data-slide-id" in attr or "data-layout" in attr:
                self.deck_markers += 1
        elif tag_l == "meta" and attr.get("name", "").lower() == "viewport":
            self.viewport_content = attr.get("content", "")
        elif tag_l == "link":
            rels = set(attr.get("rel", "").lower().split())
            href = attr.get("href", "").strip()
            if "manifest" in rels and href:
                self.manifest_hrefs.append(href)


def _parse_html(html_text: str) -> _MediumHTMLParser:
    parser = _MediumHTMLParser()
    try:
        parser.feed(html_text[:500_000])
    except Exception:
        pass
    return parser


def html_manifest_hrefs(html_text: str) -> tuple[str, ...]:
    """Return manifest hrefs declared by the HTML, if any."""

    return tuple(_parse_html(html_text).manifest_hrefs)


def detect_html_medium(
    html_text: str,
    *,
    manifest_present: bool = False,
) -> VerifierMediumHint | None:
    """Classify an HTML artifact for verifier review guidance.

    ``None`` means the normal web verifier prompt should remain unchanged.
    """

    parser = _parse_html(html_text)
    if parser.deck_markers:
        return VerifierMediumHint(
            kind="deck",
            reason="html contains slide section markers",
            review_guidance=DECK_REVIEW_GUIDANCE,
        )
    viewport = parser.viewport_content.lower()
    if manifest_present and _MAX_SCALE_ONE_RE.search(viewport):
        return VerifierMediumHint(
            kind="mobile",
            reason="viewport maximum-scale=1 and manifest are present",
            viewport_width=390,
            viewport_height=844,
            review_guidance=MOBILE_REVIEW_GUIDANCE,
        )
    return None


__all__ = [
    "DECK_REVIEW_GUIDANCE",
    "MOBILE_REVIEW_GUIDANCE",
    "VerifierMediumHint",
    "detect_html_medium",
    "html_manifest_hrefs",
]
