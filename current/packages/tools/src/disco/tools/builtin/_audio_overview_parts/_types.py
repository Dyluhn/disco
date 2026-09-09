"""Shared types for the audio-overview turn-script pipeline.

``Turn``, ``LLMResponse``/``LLMResponseLike``, ``AudioScriptGenerationError``,
``ScriptResult`` and the per-mode length band are used across payload
construction, validation, and the assembly loop, so they live here with no
dependencies of their own to avoid any import cycle between those siblings.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field


class Turn(BaseModel):
    speaker: str = Field(pattern=r"^(A|B)$")
    text: str = Field(min_length=1)


@dataclass(frozen=True)
class LLMResponse:
    """The provider fields needed to distinguish complete output from truncation."""

    content: str
    finish_reason: str | None


type LLMResponseLike = str | LLMResponse


class AudioScriptGenerationError(RuntimeError):
    """A bounded script-generation attempt could not produce a complete script."""


# The finished overview's length requirement, per mode. It is OURS, not the
# driver's plan: the model may propose a total, but the band decides. Defined
# here so payload construction, harvesting and the assembly loop cannot drift.
_TURN_BANDS: dict[str, tuple[int, int]] = {
    "single": (10, 16),
    "podcast": (12, 20),
}

# The fewest turns that still make a usable overview. Below this the assembled
# script is discarded in favour of the deterministic report-derived fallback.
_MIN_VIABLE_TURNS: dict[str, int] = {"single": 1, "podcast": 2}


def _turn_band(mode: str) -> tuple[int, int]:
    return _TURN_BANDS.get(mode, _TURN_BANDS["podcast"])


def _min_viable_turns(mode: str) -> int:
    return _MIN_VIABLE_TURNS.get(mode, 2)


@dataclass(frozen=True)
class ScriptResult:
    """A finished turn-script plus how it was obtained.

    ``source`` is ``"model"`` (the driver wrote the whole thing), ``"partial"``
    (the driver wrote a usable but short script and the budget ran out), or
    ``"fallback"`` (the driver produced nothing usable and the script was built
    deterministically from the report's own sections). ``note`` is the single
    plain sentence the UI shows when the script is not purely the model's --
    empty for ``"model"``.
    """

    turns: list[Turn]
    source: str = "model"
    note: str = ""
