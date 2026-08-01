"""JSON extraction + AuthoredDeck validation for the C2 pipeline.

Extracted from ``_slides_pipeline.py``. Both names are re-exported as
module-level attributes of ``_slides_pipeline`` and ``_extract_json_object`` is
imported directly by ``find_and_edit.py`` and tests.
"""

from __future__ import annotations

import json
import re

from disco.tools.builtin._deck_schema import AuthoredDeck
from pydantic import ValidationError

from ._craft import _coerce_known_theme_aliases, _null_invalid_layout_hints


def _extract_json_object(text: str) -> str:
    """Extract the first {...} JSON object from LLM output that may have prose."""
    text = text.strip()
    # Direct JSON object
    if text.startswith("{"):
        depth = 0
        end = 0
        for i, ch in enumerate(text):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            return text[:end]
    # Markdown code fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Last-resort: first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _parse_authored_deck(raw: str) -> tuple[AuthoredDeck | None, str]:
    """Parse raw LLM output as an AuthoredDeck.  Returns (deck, error_msg).

    Applies ``_coerce_known_theme_aliases`` BEFORE pydantic validation to
    silently fix short-hand aliases (``"dark"`` → ``"disco-dark"`` etc.).
    Unknown invalid theme strings are left for pydantic to reject; the caller
    should then pass ``_retry_msg(err)`` so the model sees the full enum.
    """
    extracted = _extract_json_object(raw)
    try:
        data = json.loads(extracted)
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    data = _coerce_known_theme_aliases(data)
    data = _null_invalid_layout_hints(data)
    try:
        deck = AuthoredDeck.model_validate(data)
    except (ValidationError, Exception) as e:
        return None, f"Schema validation error: {e}"
    return deck, ""
