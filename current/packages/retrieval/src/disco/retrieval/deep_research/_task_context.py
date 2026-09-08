"""The user assignment carried from research into writing and review."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

# The original question is always carried separately and is never cut here.
# Steering is additive and can arrive repeatedly during a long run, so keep the
# newest accepted guidance inside one explicit, visible envelope.
_STEERING_CHAR_BUDGET = 6_000


def accepted_steering(trail: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """Return accepted user steering in arrival order from the durable trail."""
    return tuple(
        text
        for row in trail
        if row.get("kind") == "steer"
        and isinstance(row.get("text"), str)
        and (text := str(row["text"]).strip())
    )


def accepted_steering_context(trail: Sequence[Mapping[str, Any]]) -> str:
    """Render bounded steering while always retaining the newest item.

    When older items do not fit, the omission is stated. This keeps the user
    assignment visible without letting an arbitrarily long steering history
    crowd the evidence or report out of the model context.
    """
    steering = accepted_steering(trail)
    if not steering:
        return ""

    heading = "ACCEPTED USER GUIDANCE ADDED DURING RESEARCH (part of the assignment):\n"
    # Reserve room for the explanatory footer too: the bound covers the entire
    # rendered context, not only complete steering items.
    body_budget = _STEERING_CHAR_BUDGET - len(heading) - 200
    selected: list[str] = []
    used = 0
    omitted = 0
    truncated = False
    for index, text in enumerate(reversed(steering)):
        line = f"- {text}"
        separator = 1 if selected else 0
        if used + separator + len(line) <= body_budget:
            selected.append(line)
            used += separator + len(line)
            continue
        if not selected:
            suffix = " … [truncated to the steering context bound]"
            room = max(0, body_budget - len("- ") - len(suffix))
            selected.append(f"- {text[:room].rstrip()}{suffix}")
            truncated = True
        omitted = len(steering) - index - int(truncated)
        break

    selected.reverse()
    omission = (
        f"\n[{omitted} earlier accepted steering item"
        f"{'s were' if omitted != 1 else ' was'} omitted by the "
        "6,000-character steering bound.]"
        if omitted
        else ""
    )
    return heading + "\n".join(selected) + omission + "\n\n"


def research_reference_context(trail: Sequence[Mapping[str, Any]]) -> str:
    """Carry the original request date across research, review and recovery."""
    for row in trail:
        if row.get("kind") != "research_reference":
            continue
        value = row.get("date")
        if not isinstance(value, str):
            continue
        try:
            reference = date.fromisoformat(value).isoformat()
        except ValueError:
            continue
        return (
            f"REFERENCE DATE (UTC, original user request): {reference}. "
            "Use an explicitly requested historical or future date when the task gives one. "
            "For current recommendations, check whether dated observations still apply; "
            "distinguish current deployment from historical conditions and projections.\n\n"
        )
    return ""


__all__ = ["accepted_steering", "accepted_steering_context", "research_reference_context"]
