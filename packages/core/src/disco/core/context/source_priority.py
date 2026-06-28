"""SourceKind + SourcePriority — the precedence ordering of context sources.

Pure value objects. The ContextPack assembler (CXT-4) uses this ordering to lay
out the model-facing context; the compaction policy uses SourceKind to decide
what may be omitted.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class SourceKind(str, Enum):
    """Every distinct kind of context source the runtime tracks."""

    GOAL = "goal"
    CONTRACT = "contract"
    TODO = "todo"
    VERIFIER_FAILURE = "verifier_failure"
    DIRECT_EDIT = "direct_edit"
    RESOURCE = "resource"
    COMMENT = "comment"
    HANDOFF = "handoff"
    RECOVERABLE_REF = "recoverable_ref"
    HISTORY = "history"


# The documented default precedence, most-important first. Goal/contract anchor
# the run; an unresolved verifier failure is the most actionable thing the model
# can do next, so it ranks above the live todo. Raw history is last.
_DEFAULT_ORDER: tuple[SourceKind, ...] = (
    SourceKind.GOAL,
    SourceKind.CONTRACT,
    SourceKind.VERIFIER_FAILURE,
    SourceKind.TODO,
    SourceKind.DIRECT_EDIT,
    SourceKind.COMMENT,
    SourceKind.RESOURCE,
    SourceKind.RECOVERABLE_REF,
    SourceKind.HANDOFF,
    SourceKind.HISTORY,
)


class SourcePriority(BaseModel):
    """An ordered precedence over SourceKind. Every kind appears exactly once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order: tuple[SourceKind, ...] = _DEFAULT_ORDER

    @classmethod
    def default(cls) -> SourcePriority:
        return cls(order=_DEFAULT_ORDER)

    def rank(self, kind: SourceKind) -> int:
        """Position of ``kind`` in the ordering (lower = higher priority)."""
        return self.order.index(kind)
