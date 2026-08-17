"""Shared reliability runner value helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _split_values(values: list[str] | None) -> set[str] | None:
    if not values:
        return None
    split = {part.strip() for value in values for part in value.split(",") if part.strip()}
    return split or None


def _expand(value: str, context: dict[str, str]) -> str:
    try:
        return value.format_map(context)
    except KeyError as exc:
        raise ValueError(f"unknown reliability matrix placeholder: {exc.args[0]}") from exc
