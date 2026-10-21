"""Shared validation for unique canonical identifiers in AppKit specs."""


def _require_unique(ids: object, *, what: str) -> None:
    """Raise ValueError on the first duplicate in `ids` (an iterable of str)."""
    seen: set[str] = set()
    for item in ids:  # type: ignore[attr-defined]
        if item in seen:
            raise ValueError(f"duplicate {what}: {item!r}")
        seen.add(item)
