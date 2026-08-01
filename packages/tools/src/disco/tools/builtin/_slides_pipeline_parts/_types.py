"""Shared types for the C2 staged deck-generation pipeline.

Extracted from ``_slides_pipeline.py`` with no dependencies of its own, so
sibling parts modules (and the parent) can import from here without any cycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMResponse:
    """Provider fields needed to distinguish complete output from truncation."""

    content: str
    finish_reason: str | None


type LLMResponseLike = str | LLMResponse


class SlidesGenerationIncompleteError(RuntimeError):
    """The provider explicitly reported that a slide-author response was incomplete."""

    def __init__(self, stage: str, finish_reason: str) -> None:
        if finish_reason == "length":
            detail = "truncated"
            code = "SLIDES_PROVIDER_OUTPUT_TRUNCATED"
        else:
            detail = "did not complete"
            code = "SLIDES_PROVIDER_OUTPUT_INCOMPLETE"
        super().__init__(
            f"{code}: Provider {detail} the slide {stage} response "
            f"(finish_reason={finish_reason!r}); the incomplete response was not "
            "retried at the same output cap, no partial deck was written, and no "
            "plain-renderer fallback was substituted. Reduce the requested slide "
            "count or goal scope, or select a model that can return the complete "
            "authored-deck JSON, then regenerate"
        )
        self.code = code
        self.stage = stage
        self.finish_reason = finish_reason
