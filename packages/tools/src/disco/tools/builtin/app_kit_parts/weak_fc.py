"""Weak function-calling (weak-FC) array-wrapper normalization.

Live 2026-07-03: with prose-only array shape guidance, MiniMax wrapped a list
as ``{"item": [...]}`` / ``{"items": [...]}`` and pydantic rejected the call.
These helpers unwrap that shape wherever an AppKit tool accepts array-bearing
content, before validation.
"""

from __future__ import annotations

from typing import Any

from disco.core.appkit.spec import Section


def _unwrap_weak_fc_items_wrapper(value: Any) -> Any:
    """Unwrap MiniMax weak-FC array wrappers at the `items` slot only.

    Live 2026-07-03: with prose-only array shape guidance, MiniMax wrapped the
    list as {"item": [...]} / {"items": [...]} and pydantic rejected the call.
    """
    if isinstance(value, dict) and len(value) == 1:
        key, wrapped = next(iter(value.items()))
        if key in {"item", "items"} and isinstance(wrapped, list):
            return wrapped
    return value


def _normalize_content_items_for_weak_fc(content: Any) -> Any:
    if not isinstance(content, dict):
        return content
    if len(content) == 1:
        key, wrapped = next(iter(content.items()))
        if key == "item" and isinstance(wrapped, list):
            return {"items": wrapped}

    raw_items = content.get("items")
    normalized_items = _unwrap_weak_fc_items_wrapper(raw_items)
    if normalized_items is raw_items:
        return content
    return {**content, "items": normalized_items}


def _normalize_section_content_for_weak_fc(section: dict[str, Any]) -> dict[str, Any]:
    content = section.get("content")
    normalized_content = _normalize_content_items_for_weak_fc(content)
    if normalized_content is content:
        return section
    return {**section, "content": normalized_content}


def _section_validation_input(section: object) -> object:
    if isinstance(section, Section):
        return section.model_dump(mode="json")
    if isinstance(section, dict):
        return _normalize_section_content_for_weak_fc(section)
    return section
