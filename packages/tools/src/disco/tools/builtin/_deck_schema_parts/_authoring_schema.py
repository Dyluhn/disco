"""Layer 1 — the loose authoring schema the LLM emits, plus the resolved-layout
and archetype vocabularies shared with Layer 2/3.

Field set FROZEN by the C4 experiment verdict (current/docs/slides-experiment-verdict.md
§6). Do NOT change the AuthoredSlide field set without a new experiment.

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# LayoutHint — resolved layout values (never None in Layer 2)
# ---------------------------------------------------------------------------

LayoutHint = Literal[
    "title",
    "section_header",
    "bullets",
    "two_column",
    "comparison",
    "image_right",
    "image_left",
    "full_image",
    "metrics",
    "metrics_grid",
    "table",
    "closing",
]

SlideArchetype = Literal[
    "title",
    "section_divider",
    "big_number",
    "full_bleed_image",
    "quote",
    "comparison_table",
    "timeline",
    "diagram",
    "two_by_two",
    "photo_grid",
    "bullets",
    "closing",
]


# ---------------------------------------------------------------------------
# Layer 1 — authoring schema (frozen by C4 verdict)
# ---------------------------------------------------------------------------


class ChartSpec(BaseModel):
    """Structured chart payload (wired in C8).

    Field set frozen by the C4 experiment verdict (current/docs/slides-experiment-verdict.md §6).
    labels / series have empty-list defaults so ChartSpec(kind=...) is valid with
    no data (c8 rendering handles empty data gracefully with a placeholder shape).
    """

    kind: Literal["bar", "line", "pie", "scatter"]
    title: str = ""
    labels: list[str] = Field(default_factory=list)
    series: list[dict] = Field(default_factory=list)  # [{"name": str, "data": [float]}]


class TableSpec(BaseModel):
    """Structured table payload.

    headers / rows have empty-list defaults for the same reason as ChartSpec.
    """

    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class AccentSpec(BaseModel):
    """One named accent in the deck's authoring-time palette."""

    name: str = Field(description="Short semantic name, e.g. 'teal' or 'signal'.")
    value: str = Field(description="CSS color token, preferably OKLCH-friendly hex/oklch.")
    role: str = Field(default="", description="Where this accent should be used.")


class FontPairingSpec(BaseModel):
    """The deck's committed font pairing for title/body/UI decisions."""

    display: str = Field(description="Display/title face; avoid Inter/Roboto/Arial defaults.")
    body: str = Field(description="Reading/body face; avoid Inter/Roboto/Arial defaults.")
    ui: str = Field(default="", description="Optional UI/label face.")


class LightDarkTokenPair(BaseModel):
    """Light and dark token pair committed by the deck author."""

    light_bg: str = Field(description="Light-mode background token.")
    light_text: str = Field(description="Light-mode text token.")
    dark_bg: str = Field(description="Dark-mode background token.")
    dark_text: str = Field(description="Dark-mode text token.")


class AuthoredSlide(BaseModel):
    """One slide as the LLM authors it — loose/semantic, no coordinates.

    Field set is FROZEN by the C4 experiment verdict.  Do NOT add or remove
    fields without running a new experiment and updating that document.
    """

    type: str = Field(
        description=(
            "Semantic slide type.  Well-known values: 'title', 'bullets', "
            "'two_column', 'metrics', 'section_header', 'image_right', "
            "'image_left', 'table', 'closing'.  Custom strings are OK — "
            "the lowerer falls back to 'bullets' for unknown types."
        )
    )
    title: str = Field(description="Slide heading.  No hard limit — lowerer truncates if needed.")
    body: list[str] = Field(
        default=[],
        description=(
            "Bullet points or content lines.  Guidelines: 4–6 lines, each "
            "under 100 chars.  If image_prompt is set and this slide is "
            "primarily visual, set body=[] or at most one caption line."
        ),
    )
    layout_hint: LayoutHint | None = Field(
        default=None,
        description=(
            "Optional explicit layout override.  If absent, _infer_layout() "
            "derives it from type + field presence."
        ),
    )
    image_prompt: str | None = Field(
        default=None,
        description=(
            "If set, image_generate is called with this prompt.  "
            "Triggers image_right/image_left or full_image layout.  "
            "Keep body=[] for full-image slides."
        ),
    )
    chart: ChartSpec | None = Field(
        default=None,
        description="Structured chart data (C8).  Triggers metrics/chart layout.",
    )
    table: TableSpec | None = Field(
        default=None,
        description="Structured table data.  Triggers table layout.",
    )
    notes: str | None = Field(
        default=None,
        description=(
            "Speaker notes.  NEVER rendered on the visible slide face.  "
            "NEVER counted in overflow calculations."
        ),
    )
    archetype: SlideArchetype | None = Field(
        default=None,
        description=(
            "Presentation archetype chosen in the outline stage: title, "
            "section_divider, big_number, full_bleed_image, quote, "
            "comparison_table, timeline, diagram, two_by_two, photo_grid, "
            "bullets, or closing."
        ),
    )


class AuthoredDeck(BaseModel):
    """A complete deck as the LLM authors it."""

    title: str
    # The deck's base template. The user can override it at export time via the
    # template selector; this is the agent's authoring-time default.
    theme: Literal[
        "disco-light",
        "disco-dark",
        "ink-light",
        "sepia-light",
        "signal-light",
        "midnight-dark",
        "neutral",
        "neutral-light",
    ] = "disco-light"
    accent_palette: list[AccentSpec] = Field(
        default_factory=list,
        description="Three or four named accents for per-section derivation.",
    )
    font_pairing: FontPairingSpec | None = Field(
        default=None,
        description="Committed non-default display/body/UI font pairing.",
    )
    token_pair: LightDarkTokenPair | None = Field(
        default=None,
        description="Committed light/dark background and text token pair.",
    )
    art_direction: str | None = Field(
        default=None,
        description=(
            "Project-wide image style contract: style keywords, palette-locked "
            "descriptors, medium, lighting, and 'no text' instruction."
        ),
    )
    slides: list[AuthoredSlide]
