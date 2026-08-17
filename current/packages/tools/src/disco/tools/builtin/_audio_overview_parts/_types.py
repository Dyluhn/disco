"""Shared types for the audio-overview turn-script pipeline.

``Turn``, ``LLMResponse``/``LLMResponseLike``, ``AudioScriptGenerationError`` and
``_ScriptBatch`` are used across payload construction, validation, and the
segmented-generation loop, so they live here with no dependencies of their own
to avoid any import cycle between those sibling modules.
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


@dataclass(frozen=True)
class _ScriptBatch:
    total_turns: int
    turns: list[Turn]
