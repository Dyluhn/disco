"""Layer 3 — editor geometry (percentage-based, consumed by React DeckEditor).

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ElementGeometry:
    """Position + size as percentage of the 16:9 slide canvas (0-100)."""

    x: float
    y: float
    w: float
    h: float


@dataclass
class LoweredElement:
    """One element as the React editor sees it — percentage geometry + JSON pointer."""

    element_id: str  # e.g. "slide-0:title"
    slide_id: str  # e.g. "slide-0"
    kind: str  # "title" | "subtitle" | "bullet" | "chart" | "table" | "image_prompt"
    content: str  # display text
    geometry: ElementGeometry
    font_size_vw: float  # font-size in vw units
    font_weight: str  # "normal" | "bold"
    font_style: str  # "normal" | "italic"
    json_pointer: str  # RFC-6901 path into AuthoredDeck (e.g. "/slides/0/title")


@dataclass
class LoweredSlide:
    """One slide for the editor — geometry in % coordinates."""

    slide_id: str
    slide_idx: int
    layout: str
    bg_color: str
    elements: list[LoweredElement] = field(default_factory=list)


@dataclass
class LoweredDeck:
    """Full lowered deck for the React editor."""

    title: str
    theme_name: str
    theme_mode: str
    slides: list[LoweredSlide] = field(default_factory=list)
