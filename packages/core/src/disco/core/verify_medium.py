"""Medium hints for visual artifact verification.

The host verifier receives bounded evidence, not the builder transcript. These
helpers keep medium detection deterministic and cheap so prompt guidance can
judge slide decks and mobile/PWA surfaces by the right rules.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from html.parser import HTMLParser
from typing import Literal

from pydantic import BaseModel, ConfigDict


class VerifierMediumHint(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["web", "deck", "mobile", "game"] = "web"
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

GAME_REVIEW_GUIDANCE = (
    "Medium: game. Do not stop at first render: use the browser tool to interact "
    "with primary input (click, Space, or arrows), capture before/after screenshots, "
    "and judge input response, visible feedback/animation between frames, plus "
    "score or failure states."
)

_MAX_SCALE_ONE_RE = re.compile(r"(?:^|[,;\s])maximum-scale\s*=\s*1(?:\.0+)?(?:$|[,;\s])")


class _MediumHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.deck_markers = 0
        self.canvas_markers = 0
        self.viewport_content = ""
        self.manifest_hrefs: list[str] = []
        self.script_srcs: list[str] = []
        self.script_text: list[str] = []
        self._script_depth = 0

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
        elif tag_l == "canvas":
            self.canvas_markers += 1
        elif tag_l == "script":
            self._script_depth += 1
            src = attr.get("src", "").strip()
            if src:
                self.script_srcs.append(src)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._script_depth:
            self._script_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._script_depth and data:
            self.script_text.append(data[:50_000])


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


def html_script_srcs(html_text: str) -> tuple[str, ...]:
    """Return local script srcs declared by the HTML, if any."""

    return tuple(_parse_html(html_text).script_srcs)


def _has_game_markers(
    parser: _MediumHTMLParser,
    html_text: str,
    related_texts: Sequence[str],
    *,
    game_marker_present: bool,
) -> bool:
    evidence_parts = [html_text, "\n".join(parser.script_text), *related_texts]
    evidence = "\n".join(evidence_parts)[:500_000].lower()
    has_canvas = parser.canvas_markers > 0 or "<canvas" in evidence
    has_raf = "requestanimationframe" in evidence
    has_key_listener = "keydown" in evidence or "keyup" in evidence
    return (
        game_marker_present
        or "game_loop_vanilla" in evidence
        or (has_canvas and (has_raf or has_key_listener))
    )


def detect_html_medium(
    html_text: str,
    *,
    manifest_present: bool = False,
    game_marker_present: bool = False,
    related_texts: Sequence[str] = (),
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
    if _has_game_markers(
        parser,
        html_text,
        related_texts,
        game_marker_present=game_marker_present,
    ):
        return VerifierMediumHint(
            kind="game",
            reason="workspace contains canvas game loop markers",
            review_guidance=GAME_REVIEW_GUIDANCE,
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
    "GAME_REVIEW_GUIDANCE",
    "MOBILE_REVIEW_GUIDANCE",
    "VerifierMediumHint",
    "detect_html_medium",
    "html_manifest_hrefs",
    "html_script_srcs",
]
