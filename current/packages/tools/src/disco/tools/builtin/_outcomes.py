"""Shared ToolOutcome helpers for built-in tools."""

from __future__ import annotations

from typing import Any

from ..anatomy import ToolOutcome


def fail_outcome(
    message: str,
    *,
    error: str | None = None,
    structured: dict[str, Any] | None = None,
    artifacts: list[str] | None = None,
) -> ToolOutcome:
    """Return a failed outcome whose model-facing content is never empty."""
    content = message or error or "Tool failed."
    return ToolOutcome(
        success=False,
        content=content,
        error=error if error is not None else content,
        structured=structured,
        artifacts=artifacts or [],
    )
