"""A coherent writing boundary persisted by the existing research checkpoint owner."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._output_ceiling import RESEARCH_CEILING_CAP
from ._review_protocol import ReviewBudget
from ._source_notes import SourceNotes
from ._writer_findings import Finding
from ._writer_parts import FinalReport


class WriterCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_sha256: str = ""
    research_sha256: str = ""
    final: FinalReport
    draft_sha256: str
    stage: Literal["reading", "review", "repair", "final_check", "complete"]
    budget: ReviewBudget
    findings: list[Finding] = Field(default_factory=list)
    review_outcome: str = "unavailable"
    review_trail: dict[str, Any] = Field(default_factory=dict)
    trail: list[dict[str, Any]] = Field(default_factory=list)
    draft_trail: list[dict[str, Any]] = Field(default_factory=list)
    repair_started: bool = False
    # Validated notes from reading each long source whole, keyed by passage
    # id. Defaulted so a checkpoint written before this stage still loads;
    # a resume that carries notes never pays for the reading twice.
    source_notes: dict[str, SourceNotes] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent_boundary(self) -> WriterCheckpoint:
        if self.draft_sha256 != hashlib.sha256(self.final.markdown.encode()).hexdigest():
            raise ValueError("writer checkpoint does not match its draft")
        if not 0 <= self.budget.used <= self.budget.limit:
            raise ValueError("writer checkpoint exceeds its review allowance")
        if not 0 <= self.budget.output_ceiling <= RESEARCH_CEILING_CAP:
            raise ValueError("writer checkpoint exceeds its review output capacity")
        if not 0 <= self.budget.final_reserve <= 2:
            raise ValueError("writer checkpoint exceeds its final review reserve")
        return self


WriterCheckpointFn = Callable[[WriterCheckpoint], Awaitable[None]]
ReviewCheckpointFn = Callable[[], Awaitable[None]]


def bind_writer_checkpoint(
    checkpoint: WriterCheckpointFn | None,
    signature: str,
    research_signature: str,
    trail: list[dict[str, Any]],
    source_notes: dict[str, SourceNotes],
) -> WriterCheckpointFn:
    """Attach the immutable inputs and completed reading to each writing boundary."""

    async def save(state: WriterCheckpoint) -> None:
        state.input_sha256, state.draft_trail = signature, list(trail)
        state.research_sha256, state.source_notes = research_signature, source_notes
        if checkpoint is not None:
            await checkpoint(state)

    return save


def writer_input_signature(
    query: str, passages: list[Any], coverage: dict[str, Any], steering: str
) -> str:
    data = {
        "query": query,
        "coverage": coverage,
        "steering": steering,
        "sources": [
            (p.id, hashlib.sha256(p.model_dump_json().encode()).hexdigest()) for p in passages
        ],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reading_checkpoint(
    checkpoint: WriterCheckpointFn | None,
    signature: str,
    research_signature: str,
    limit: int,
    *,
    final_reserve: int = 1,
) -> Callable[[dict[str, SourceNotes]], Awaitable[None]]:
    """Commit reading through the same owner as draft/review recovery."""

    async def save(notes: dict[str, SourceNotes]) -> None:
        if checkpoint is None:
            return
        empty = FinalReport(title="", summary="", sections=())
        await checkpoint(
            WriterCheckpoint(
                input_sha256=signature,
                research_sha256=research_signature,
                final=empty,
                draft_sha256=hashlib.sha256(empty.markdown.encode()).hexdigest(),
                stage="reading",
                budget=ReviewBudget(limit=limit, final_reserve=final_reserve),
                source_notes=notes,
            )
        )

    return save
