"""Small standalone helpers shared across the deck-schema lowering pipeline:
opaque id generation, CSS font-stack parsing, and theme-id splitting.

Extracted from ``_deck_schema.py`` to reduce module size; the public facade
re-imports these names unchanged.
"""

from __future__ import annotations

import uuid

from disco.core.brand import parse_template_id


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _first_font(stack: str) -> str:
    """Extract the first font name from a CSS font-family stack."""
    return stack.split(",")[0].strip().strip("'\"")


def _parse_theme(theme_str: str) -> tuple[str, str]:
    """Split a "{name}-{mode}" template id → (name, mode). Delegates to the brand
    catalogue's generalized parser so ANY registered template (disco/ink/sepia/
    signal/midnight/neutral…) works as a deck theme, not just the original three.
    resolve_theme takes two args, so callers split rather than pass the compound."""
    return parse_template_id(theme_str)
