"""Three emit strategies for the C4 slides schema experiment.

Each strategy returns a (system_prompt, user_prompt_template) pair.
The user_prompt_template has a single {goal} placeholder.

Strategy A: FREE_FORM
  Model can emit any structure it wants — JSON, markdown, mixed.
  No schema constraints. Tests unconstrained quality.

Strategy B: RIGID_JSON
  Model must emit a strict JSON schema with fixed layout enum
  and field caps. Tests maximum determinism.

Strategy C: LOOSE_HYBRID
  Model emits instructed JSON following an AuthoredDeck schema,
  but the schema is permissive — layout_hint is optional,
  no per-field caps enforced. Tests the proposed default approach.
"""

from __future__ import annotations

from dataclasses import dataclass

STRATEGY_IDS = ("free_form", "rigid_json", "loose_hybrid")


@dataclass(frozen=True)
class Strategy:
    id: str
    label: str
    system: str
    user_template: str  # single {goal} placeholder


# ---------------------------------------------------------------------------
# A — FREE_FORM
# ---------------------------------------------------------------------------
_FREE_SYSTEM = """\
You are a professional slide deck designer and writer.
Create compelling, well-structured slide decks based on the user's request.
Output your response as a complete slide deck — you may use any format you
prefer: JSON, markdown with slide separators, or a mixed representation.
Focus on quality, coherence, and fitting the content to appropriate slides.
Do NOT add meta-commentary — just output the deck."""

_FREE_USER = """\
Create a slide deck for the following goal:

{goal}

Output the complete deck in whatever format best captures the content.
Be thorough, creative, and professional. Include all the slides and content
described. Aim for quality over brevity."""


# ---------------------------------------------------------------------------
# B — RIGID_JSON
# ---------------------------------------------------------------------------
_RIGID_SYSTEM = """\
You are a slide deck generator. You MUST output ONLY valid JSON conforming
exactly to this schema — no prose, no markdown, just the JSON object:

{
  "title": string,           // deck title, max 80 chars
  "theme": "light" | "dark" | "neutral",
  "slides": [
    {
      "type": "title" | "bullets" | "two_column" | "metrics" | "section_header" | "image" | "table" | "closing",
      "title": string,       // slide title, max 60 chars STRICTLY
      "bullets": [string],   // list of bullet points, max 6 items, each max 80 chars STRICTLY
      "left_bullets": [string],  // for two_column only, max 4 items, each max 80 chars
      "right_bullets": [string], // for two_column only, max 4 items, each max 80 chars
      "metrics": [{"label": string, "value": string}],  // for metrics only, max 4 metrics
      "image_description": string | null,  // one sentence, max 120 chars
      "table": {"headers": [string], "rows": [[string]]}  // max 5 cols, max 6 rows
    }
  ]
}

RULES (ENFORCE STRICTLY):
- title field: MAX 60 chars
- bullets: MAX 6 per slide, each MAX 80 chars
- left_bullets/right_bullets: MAX 4 each, each MAX 80 chars
- metrics: MAX 4 items
- table: MAX 5 columns, MAX 6 rows
- Only use the listed type values — no custom types
- Omit fields that don't apply (null fields allowed)
- Output ONLY the JSON — no explanation, no markdown fencing"""

_RIGID_USER = """\
Generate a slide deck for:

{goal}

Output ONLY the JSON. No prose. No markdown fencing. Just the raw JSON object."""


# ---------------------------------------------------------------------------
# C — LOOSE_HYBRID
# ---------------------------------------------------------------------------
_LOOSE_SYSTEM = """\
You are a slide deck author. Output a JSON object following the AuthoredDeck
schema below — instructed JSON, not grammar-constrained, so you have some
flexibility but must produce parseable JSON.

AuthoredDeck schema:
{
  "title": string,
  "theme": "disco-light" | "disco-dark" | "neutral",
  "slides": [AuthoredSlide, ...]
}

AuthoredSlide schema:
{
  "type": string,            // semantic type: "title", "bullets", "two_column",
                             //   "metrics", "section_header", "image_right",
                             //   "image_left", "table", "closing", or custom
  "title": string,           // slide title
  "body": [string],          // bullet points or content lines (keep concise)
  "layout_hint": string | null,  // optional: "two_column", "metrics_grid",
                                 //   "image_right", "image_left", "section_header"
  "image_prompt": string | null, // if the slide needs an image, describe it here
  "chart": {                 // optional chart data
    "kind": "bar" | "line" | "pie" | "scatter",
    "title": string,
    "labels": [string],
    "series": [{"name": string, "data": [number]}]
  } | null,
  "table": {                 // optional table
    "headers": [string],
    "rows": [[string]]
  } | null,
  "notes": string | null     // speaker notes
}

GUIDELINES (not hard limits — use your judgment):
- body: aim for 4-6 concise bullets; more is OK if the content demands it
- title: aim for under 70 chars
- Use layout_hint to signal two-column, image, or metrics layouts
- Set image_prompt when an image would enhance the slide
- Omit null fields if not needed
- Output ONLY the JSON object — no prose, no markdown fencing"""

_LOOSE_USER = """\
Create a slide deck for the following:

{goal}

Think about the best layout for each slide, use image_prompt where images help,
and include chart/table data structures where the content is data-heavy.
Output ONLY the JSON."""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------
FREE_FORM = Strategy(
    id="free_form",
    label="A — Free-Form",
    system=_FREE_SYSTEM,
    user_template=_FREE_USER,
)

RIGID_JSON = Strategy(
    id="rigid_json",
    label="B — Rigid JSON",
    system=_RIGID_SYSTEM,
    user_template=_RIGID_USER,
)

LOOSE_HYBRID = Strategy(
    id="loose_hybrid",
    label="C — Loose Hybrid",
    system=_LOOSE_SYSTEM,
    user_template=_LOOSE_USER,
)

ALL_STRATEGIES: dict[str, Strategy] = {
    s.id: s for s in [FREE_FORM, RIGID_JSON, LOOSE_HYBRID]
}
