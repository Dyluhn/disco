"""Web interaction/layout rule family: reflexive hover scale, hover-only
interactivity, text-over-image scrim, glass sanity, default-hidden content,
pill-button monoculture, emoji-as-icons, and the centered-hero/3-cards/CTA
section sequence. Extracted from `design_lint.py`.

Two callables are DECOMPOSED (not just relocated) versus the original
monolith:

* `_rule_web_text_over_image_no_scrim` (16 mccabe) held two independent scans
  — a CSS-block scan and a markup-media-container scan — where the function
  returned as soon as the FIRST one matched. That short-circuit is preserved
  exactly (`_web_text_over_image_css_findings` is checked in full before
  `_web_text_over_image_markup_findings` runs at all), just as separately
  named callables instead of one big function body.
* `_rule_pill_buttons` (17 mccabe) held a CSS-block scan and a markup
  (`rounded-full`) scan that both feed the same `pill`/`first` accumulators.
  `_pill_css_scan` / `_pill_markup_scan` now own one accumulator pass each,
  threading `first` through in the same order (CSS before markup) so the
  reported evidence is identical to before.

Every finding field, message, and iteration order is unchanged."""

from __future__ import annotations

import re

from disco.core.appkit import (
    CHOICE_COMPONENT_BUTTON_RADIUS,
    CHOICE_ICONS_STYLE,
    CHOICE_LAYOUT_SECTION_SEQUENCE,
)

from ._constants import (
    _BUTTON_SELECTOR_TOKENS,
    _CHOICE_WEB_DEFAULT_HIDDEN_CONTENT,
    _CHOICE_WEB_GLASS,
    _CHOICE_WEB_HOVER_A11Y,
    _CHOICE_WEB_HOVER_SCALE,
    _CHOICE_WEB_TEXT_OVER_IMAGE,
    _CSS_BLOCK_RE,
    _EMOJI_RE,
    _EMOJI_THRESHOLD,
    _PILL_RADII,
    _STYLE_ATTR_RE,
    _WEB_CONTENT_SELECTOR_RE,
    _WEB_HEROISH_RE,
    _WEB_HIDDEN_DECL_RE,
    _WEB_JS_GATED_SELECTOR_RE,
    _WEB_MEDIA_CONTAINER_RE,
    _WEB_TEXT_TAG_RE,
)
from ._deck_slides import _attr_value
from ._model import DesignFinding
from ._scan_helpers import (
    _backdrop_without_saturate,
    _class_has_hover_utility,
    _has_background_image_url,
    _has_colorful_backdrop,
    _has_scrim_or_overlay,
    _inside_css_guard,
    _line_of,
    _selector_targets_body_or_heading,
    _selector_targets_interactive,
    _style_has_backdrop_filter,
    _style_has_flat_background,
)


def _rule_web_reflexive_hover_scale(path: str, text: str) -> list[DesignFinding]:
    selectors: dict[str, int] = {}
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).strip()
        body = m.group(2)
        if ":hover" not in selector.lower():
            continue
        if re.search(r"\btransform\s*:[^;{}]*scale(?:3d|x|y)?\(", body, re.I):
            for part in selector.split(","):
                hover_selector = part.strip()
                if ":hover" in hover_selector.lower():
                    selectors.setdefault(hover_selector, _line_of(text, m.start()))

    for m in re.finditer(r"\bclass\s*=\s*(['\"])(?P<class>.*?)\1", text, re.I | re.DOTALL):
        classes = m.group("class")
        hover_scale = [token for token in classes.split() if token.startswith("hover:scale")]
        if not hover_scale:
            continue
        selectors.setdefault(" ".join(sorted(hover_scale)), _line_of(text, m.start()))

    if len(selectors) <= 3:
        return []
    first_selector, first_line = next(iter(selectors.items()))
    return [
        DesignFinding(
            rule_id="web_reflexive_hover_scale",
            severity="warning",
            path=path,
            line=first_line,
            evidence=f"{len(selectors)} hover scale selectors; first: {first_selector[:100]}",
            choice_key=_CHOICE_WEB_HOVER_SCALE,
            message=(
                "Hover scale appears on more than three distinct selectors — the reflexive "
                "template interaction tell. Reserve scale for one or two meaningful affordances."
            ),
        )
    ]


def _rule_web_hover_only_interactivity(
    path: str, text: str, *, workspace_has_focus_visible: bool
) -> list[DesignFinding]:
    if workspace_has_focus_visible:
        return []
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).strip()
        if ":hover" not in selector.lower() or not _selector_targets_interactive(selector):
            continue
        return [
            DesignFinding(
                rule_id="web_hover_only_interactivity",
                severity="warning",
                path=path,
                line=_line_of(text, m.start()),
                evidence=selector[:120],
                choice_key=_CHOICE_WEB_HOVER_A11Y,
                message=(
                    "Interactive elements have hover styling but the workspace has no "
                    ":focus-visible state. Add keyboard-visible focus treatment."
                ),
            )
        ]

    for m in re.finditer(
        r"<(?P<tag>a|button|input|select|textarea|summary)\b(?P<attrs>[^>]*)>",
        text,
        re.I | re.DOTALL,
    ):
        attrs = m.group("attrs")
        classes = _attr_value(attrs, "class") or ""
        if not _class_has_hover_utility(classes):
            continue
        return [
            DesignFinding(
                rule_id="web_hover_only_interactivity",
                severity="warning",
                path=path,
                line=_line_of(text, m.start()),
                evidence=f'<{m.group("tag")} class="{classes[:90]}">',
                choice_key=_CHOICE_WEB_HOVER_A11Y,
                message=(
                    "Interactive elements have hover utility classes but the workspace has no "
                    "focus-visible state. Add keyboard-visible focus treatment."
                ),
            )
        ]
    return []


def _web_text_over_image_css_findings(path: str, text: str) -> list[DesignFinding]:
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).strip()
        body = m.group(2)
        if not _has_background_image_url(body):
            continue
        if (
            not _selector_targets_body_or_heading(selector)
            and _WEB_HEROISH_RE.search(selector) is None
        ):
            continue
        if _has_scrim_or_overlay(body):
            continue
        return [
            DesignFinding(
                rule_id="web_text_over_image_no_scrim",
                severity="warning",
                path=path,
                line=_line_of(text, m.start()),
                evidence=f"{selector} uses background image without gradient/overlay",
                choice_key=_CHOICE_WEB_TEXT_OVER_IMAGE,
                message=(
                    "Text appears intended over imagery without a scrim/overlay heuristic. Add "
                    "a gradient scrim, overlay layer, card, or blur protection and verify contrast."
                ),
            )
        ]
    return []


def _web_text_over_image_markup_findings(path: str, text: str) -> list[DesignFinding]:
    for m in _WEB_MEDIA_CONTAINER_RE.finditer(text):
        attrs = m.group("attrs") or ""
        body = m.group("body") or ""
        raw = f"{attrs}\n{body}"
        styled_background = _has_background_image_url(attrs)
        layered_image = "<img" in body.lower() and _WEB_TEXT_TAG_RE.search(body) is not None
        if not styled_background and not layered_image:
            continue
        if _WEB_TEXT_TAG_RE.search(body) is None:
            continue
        if layered_image and _WEB_HEROISH_RE.search(raw) is None:
            continue
        if _has_scrim_or_overlay(raw):
            continue
        return [
            DesignFinding(
                rule_id="web_text_over_image_no_scrim",
                severity="warning",
                path=path,
                line=_line_of(text, m.start()),
                evidence=f"<{m.group('tag')}{attrs[:100]}>",
                choice_key=_CHOICE_WEB_TEXT_OVER_IMAGE,
                message=(
                    "Text appears over background/image media without a scrim/overlay sibling. "
                    "Add a protection layer and verify worst-case contrast."
                ),
            )
        ]
    return []


def _rule_web_text_over_image_no_scrim(path: str, text: str) -> list[DesignFinding]:
    css_findings = _web_text_over_image_css_findings(path, text)
    if css_findings:
        return css_findings
    return _web_text_over_image_markup_findings(path, text)


def _rule_web_glass(path: str, text: str) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    glass_uses: list[tuple[int, str]] = []

    for m in _CSS_BLOCK_RE.finditer(text):
        body = m.group(2)
        if not _style_has_backdrop_filter(body):
            continue
        evidence = _backdrop_without_saturate(body)
        if evidence is not None:
            findings.append(
                DesignFinding(
                    rule_id="web_glass_missing_saturate",
                    severity="warning",
                    path=path,
                    line=_line_of(text, m.start()),
                    evidence=evidence,
                    choice_key=_CHOICE_WEB_GLASS,
                    message="backdrop-filter without saturate() makes site glass read as gray mud",
                )
            )
        glass_uses.append((m.start(), evidence or "backdrop-filter"))

    for m in _STYLE_ATTR_RE.finditer(text):
        style = m.group("style")
        if not _style_has_backdrop_filter(style):
            continue
        evidence = _backdrop_without_saturate(style)
        if evidence is not None:
            findings.append(
                DesignFinding(
                    rule_id="web_glass_missing_saturate",
                    severity="warning",
                    path=path,
                    line=_line_of(text, m.start()),
                    evidence=evidence,
                    choice_key=_CHOICE_WEB_GLASS,
                    message="backdrop-filter without saturate() makes site glass read as gray mud",
                )
            )
        glass_uses.append((m.start(), evidence or "backdrop-filter"))

    if not glass_uses:
        return findings
    if _has_colorful_backdrop(text):
        return findings
    if not _style_has_flat_background(text):
        return findings

    offset, evidence = glass_uses[0]
    findings.append(
        DesignFinding(
            rule_id="web_glass_flat_backdrop",
            severity="warning",
            path=path,
            line=_line_of(text, offset),
            evidence=evidence,
            choice_key=_CHOICE_WEB_GLASS,
            message=(
                "site glass sits over a flat single-color backdrop; glass needs colorful, "
                "gradient, image, or video content behind it"
            ),
        )
    )
    return findings


def _pill_css_scan(low: str) -> tuple[int, int, tuple[int, str] | None]:
    pill = 0
    nonpill = 0
    first: tuple[int, str] | None = None
    for m in _CSS_BLOCK_RE.finditer(low):
        selector = m.group(1)
        body = m.group(2)
        if not any(tok in selector for tok in _BUTTON_SELECTOR_TOKENS):
            continue
        rm = re.search(r"border-radius\s*:\s*([^;{}]+)", body)
        if rm is None:
            continue
        value = rm.group(1).strip()
        if any(p in value for p in _PILL_RADII):
            pill += 1
            if first is None:
                first = (_line_of(low, m.start()), m.group(1).strip()[:80])
        else:
            nonpill += 1
    return pill, nonpill, first


def _pill_markup_scan(
    low: str, first: tuple[int, str] | None
) -> tuple[int, tuple[int, str] | None]:
    pill = 0
    for m in re.finditer(r'class\s*=\s*"([^"]*)"', low):
        cls = m.group(1)
        if ("btn" in cls or "button" in cls) and "rounded-full" in cls:
            pill += 1
            if first is None:
                first = (_line_of(low, m.start()), "rounded-full")
    return pill, first


def _rule_pill_buttons(path: str, text: str, justified: set[str]) -> list[DesignFinding]:
    if CHOICE_COMPONENT_BUTTON_RADIUS in justified:
        return []
    low = text.lower()
    css_pill, nonpill, first = _pill_css_scan(low)
    markup_pill, first = _pill_markup_scan(low, first)
    pill = css_pill + markup_pill
    if pill >= 2 and nonpill == 0 and first is not None:
        return [
            DesignFinding(
                rule_id="pill_button_monoculture",
                severity="info",
                path=path,
                line=first[0],
                evidence=first[1],
                choice_key=CHOICE_COMPONENT_BUTTON_RADIUS,
                message=(
                    "Every button is a full pill — a one-note component language. Vary the radius "
                    "by emphasis, or justify component.button_radius in .disco/designspec.json."
                ),
            )
        ]
    return []


def _rule_emoji_icons(path: str, text: str, justified: set[str]) -> list[DesignFinding]:
    if CHOICE_ICONS_STYLE in justified:
        return []
    matches = _EMOJI_RE.findall(text)
    if len(matches) < _EMOJI_THRESHOLD:
        return []
    first = next(iter(_EMOJI_RE.finditer(text)))
    sample = "".join(dict.fromkeys(matches[:6]))
    return [
        DesignFinding(
            rule_id="emoji_as_icons",
            severity="info",
            path=path,
            line=_line_of(text, first.start()),
            evidence=f"{len(matches)} emoji used as icons (e.g. {sample})",
            choice_key=CHOICE_ICONS_STYLE,
            message=(
                "Emoji standing in for icons — reads as a placeholder. Use a real icon set, or "
                "justify icons.style in .disco/designspec.json."
            ),
        )
    ]


def _rule_centered_hero_3_cards_cta(
    markup: list[tuple[str, str]], justified: set[str]
) -> list[DesignFinding]:
    """Workspace-level section-sequence check across the markup files."""
    if CHOICE_LAYOUT_SECTION_SEQUENCE in justified:
        return []
    centering = ("text-center", "items-center", "justify-center", "mx-auto", "text-align:center")
    for path, text in markup:
        low = re.sub(r"\s+", " ", text.lower())
        # hero (centered)
        hero = re.search(r'(<section[^>]*|class\s*=\s*"[^"]*)hero', low)
        if hero is None:
            continue
        window = low[hero.start() : hero.start() + 400]
        if not any(c in window for c in centering):
            continue
        hero_idx = hero.start()
        # exactly three cards: a 3-col grid OR exactly three card-classed elements
        cards_idx: int | None = None
        grid = re.search(r"grid-cols-3|repeat\(3,|grid-template-columns\s*:\s*repeat\(\s*3", low)
        card_iter = list(re.finditer(r'class\s*=\s*"[^"]*card', low))
        if grid is not None and grid.start() > hero_idx:
            cards_idx = grid.start()
        elif len(card_iter) == 3 and card_iter[0].start() > hero_idx:
            cards_idx = card_iter[0].start()
        if cards_idx is None:
            continue
        # a CTA strictly AFTER the cards (search the tail so a "Get started" button
        # sitting INSIDE the hero can't be mistaken for the closing CTA).
        cta = re.search(
            r'(class\s*=\s*"[^"]*cta|<section[^>]*cta|get started|sign up|start free)',
            low[cards_idx:],
        )
        if cta is None:
            continue
        return [
            DesignFinding(
                rule_id="centered_hero_3_cards_cta",
                severity="warning",
                path=path,
                line=_line_of(text, 0),
                evidence="centered hero -> 3 feature cards -> CTA",
                choice_key=CHOICE_LAYOUT_SECTION_SEQUENCE,
                message=(
                    "The centered-hero / three-cards / CTA sequence is THE generated-landing-page "
                    "shape. Compose from the section-variant catalog instead, or justify "
                    "layout.section_sequence in .disco/designspec.json."
                ),
            )
        ]
    return []


def _rule_web_default_hidden_content(path: str, text: str) -> list[DesignFinding]:
    findings: list[DesignFinding] = []
    seen: set[str] = set()
    for m in _CSS_BLOCK_RE.finditer(text):
        selector = m.group(1).strip()
        selector_low = selector.lower()
        if _inside_css_guard(text, m.start()):
            continue
        if "@media" in selector_low or "@supports" in selector_low:
            continue
        if _WEB_JS_GATED_SELECTOR_RE.search(selector) is not None:
            continue
        if _WEB_CONTENT_SELECTOR_RE.search(selector) is None:
            continue
        body = m.group(2)
        hidden = _WEB_HIDDEN_DECL_RE.search(body)
        if hidden is None:
            continue
        key = selector_low
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            DesignFinding(
                rule_id="web_default_hidden_content",
                severity="warning",
                path=path,
                line=_line_of(text, m.start(2) + hidden.start()),
                evidence=f"{selector} {{ {hidden.group(0).strip()} }}",
                choice_key=_CHOICE_WEB_DEFAULT_HIDDEN_CONTENT,
                message=(
                    "Content is hidden by default without a JS-gated ancestor or media guard. "
                    "Gate scroll-reveal initial-hidden styles behind html.js (or equivalent) "
                    "so the page fails visible when JavaScript is unavailable."
                ),
            )
        )
    return findings
