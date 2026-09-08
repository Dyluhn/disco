"""A versioned serialization boundary for the existing research state owner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import Passage, SearchHit
from ..url_policy import source_url_key
from ._agent_state import _AgentState
from ._budget import SourceBudget
from ._exhaustion import ExtractionOutcomes
from ._output_ceiling import TurnCeiling
from ._reissue_queue import QueuedQuery, ReissueQueue
from ._turn_accounting import TurnCharge, turn_charge_trail_row
from ._writer_checkpoint import WriterCheckpoint
from .depth import DepthBound


class LoopCursor(BaseModel):
    next_turn: int = Field(ge=0)
    malformed_streak: int = Field(default=0, ge=0)
    infrastructure_streak: int = Field(default=0, ge=0)
    ceiling_tokens: int = Field(gt=0)

    @classmethod
    def initial(cls, state: _AgentState, saved: LoopCursor | None) -> LoopCursor:
        """Restore execution counters, or derive the next legacy trail ordinal."""
        if saved is not None:
            return saved.model_copy()
        turn = (
            max(
                (
                    entry.get("turn", -1)
                    for entry in state.trail
                    if isinstance(entry.get("turn"), int)
                ),
                default=-1,
            )
            + 1
        )
        return cls(next_turn=turn, ceiling_tokens=TurnCeiling.start().tokens)

    async def cancel_model_turn(self, state: _AgentState, callback: CheckpointFn | None) -> None:
        """Persist the spent model turn without committing an unfinished response."""
        state.turns_charged += 1
        state.trail.append(
            turn_charge_trail_row(
                self.next_turn, TurnCharge(counted=True, reason="model_cancelled")
            )
        )
        self.next_turn += 1
        await self.commit(state, callback)

    async def commit(self, state: _AgentState, callback: CheckpointFn | None) -> None:
        if callback is not None:
            await callback(state, self)


class AgentCheckpoint(BaseModel):
    """An immutable copy at a boundary; _AgentState remains the sole live owner."""

    model_config = ConfigDict(extra="forbid")
    passages: list[Passage]
    all_hits: list[SearchHit]
    trail: list[dict[str, Any]]
    budget_ids: set[str]
    last_admitted: set[str]
    brief: str
    decision_summary: str
    coverage: dict[str, Any]
    turns_completed: int = Field(ge=0)
    turns_charged: int = Field(ge=0)
    feedback: str
    searches: int = Field(ge=0)
    queued: list[QueuedQuery]
    abandoned: list[QueuedQuery]
    extraction: ExtractionOutcomes

    @classmethod
    def capture(cls, state: _AgentState) -> AgentCheckpoint:
        return cls(
            passages=state.pool,
            all_hits=state.all_hits,
            trail=state.trail,
            budget_ids=state.budget._seen_ids,
            last_admitted=state.last_admitted,
            brief=state.brief,
            decision_summary=state.decision_summary,
            coverage=state.coverage,
            turns_completed=state.turns_completed,
            turns_charged=state.turns_charged,
            feedback=state.feedback,
            searches=state.searches,
            queued=state.reissue._items,
            abandoned=state.reissue._abandoned,
            extraction=state.extraction,
        ).model_copy(deep=True)

    def restore(self, bound: DepthBound) -> _AgentState:
        state = _AgentState(budget=SourceBudget(bound.max_sources, set(self.budget_ids)))
        state.admit_exempt(self.passages)
        state.all_hits = list(self.all_hits)
        state.seen_hit_urls = {source_url_key(hit.url) for hit in self.all_hits}
        saved = self.model_copy(deep=True)
        for name in (
            "trail",
            "last_admitted",
            "brief",
            "decision_summary",
            "coverage",
            "turns_completed",
            "turns_charged",
            "feedback",
            "searches",
            "extraction",
        ):
            setattr(state, name, getattr(saved, name))
        state.reissue = ReissueQueue(list(self.queued), list(self.abandoned))
        return state


class RecoveryCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    conversation_id: str
    run_id: str
    query: str
    depth_tier: str
    recency_window: Literal["month", "week"] | None = None
    stage: Literal["research", "writing"]
    bound: DepthBound
    state: AgentCheckpoint
    cursor: LoopCursor
    bounded_by: str | None = None
    writer: WriterCheckpoint | None = None

    @model_validator(mode="after")
    def valid_budget(self) -> RecoveryCheckpoint:
        ids = [passage.id for passage in self.state.passages]
        if len(ids) != len(set(ids)) or not self.state.budget_ids.issubset(ids):
            raise ValueError("checkpoint evidence identity or charged sources are inconsistent")
        if len(self.state.budget_ids) > self.bound.max_sources:
            raise ValueError("checkpoint exceeds its source budget")
        if self.state.turns_charged > self.bound.max_research_turns:
            raise ValueError("checkpoint exceeds its turn budget")
        if self.writer is not None and (
            self.stage != "writing" or self.writer.budget.limit != self.bound.review_decisions
        ):
            raise ValueError("writer checkpoint does not match the run stage or review budget")
        return self


CheckpointFn = Callable[[_AgentState, LoopCursor], Awaitable[None]]
