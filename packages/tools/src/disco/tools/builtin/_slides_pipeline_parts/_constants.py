"""Theme/archetype enum strings, derived from the C4 AuthoredDeck schema.

Extracted from ``_slides_pipeline.py``. Deriving these from the schema's own
Literal annotations (rather than hand-listing them) keeps every prompt and the
retry message in sync with ``_deck_schema.py`` automatically.
"""

from __future__ import annotations

from typing import get_args

from disco.tools.builtin._deck_schema import AuthoredDeck, SlideArchetype

_VALID_THEMES: tuple[str, ...] = get_args(AuthoredDeck.model_fields["theme"].annotation)
_VALID_THEMES_STR = " | ".join(f'"{t}"' for t in _VALID_THEMES)
_ARCHETYPES: tuple[str, ...] = get_args(SlideArchetype)
_ARCHETYPES_STR = " | ".join(f'"{a}"' for a in _ARCHETYPES)
