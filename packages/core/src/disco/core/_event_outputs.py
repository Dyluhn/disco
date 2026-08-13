"""Output events — proposals, knowledge, constraints, and deliverables.

These models carry structured plans and alternatives, finished research
reports, durable knowledge/data-source context, host runtime constraints, and
deliverable handoff signals.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, model_validator

from ._event_render import report_truncation
from ._event_types import (
    AlternativeOption,
    BaseEvent,
    EventKind,
    EventSource,
    LLMConvertible,
    LLMMessage,
    PlanStep,
    ReportSection,
    VerificationDeliveryContract,
)
from .dod import predicate_to_dict


class PlanEvent(BaseEvent, LLMConvertible):
    """A structured plan the agent proposed (in PLANNING mode) and the human is
    asked to approve before any work runs. LLMConvertible so the committed plan
    stays in the agent's context during execution — it renders as an assistant
    message restating the steps it agreed to carry out.

    `context` is an optional markdown body carrying the planner's findings from
    exploring the workspace + web (Claude-Code-style: explain WHY this plan, what
    you learned, the trade-offs). Empty by default for backward compatibility."""

    kind: Literal[EventKind.PLAN] = EventKind.PLAN
    source: EventSource = EventSource.AGENT
    summary: str  # one or two sentences: what this plan delivers
    steps: list[PlanStep]
    revision: int = 1  # bumps each time the user sends the plan back for changes
    context: str = ""  # optional markdown rationale + exploration findings

    def to_llm_message(self) -> LLMMessage:
        lines: list[str] = []
        for i, step in enumerate(self.steps, start=1):
            lines.append(f"{i}. {step.title}")
            if step.detail:
                lines.append(f"   Detail: {step.detail}")
            if step.done_condition is not None:
                condition = json.dumps(
                    predicate_to_dict(step.done_condition),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                lines.append(f"   Done condition: {condition}")
        body = "\n".join(lines)
        # Include the context so the executing agent has its own findings in-View
        # — same role Claude Code's plan-file markdown plays during execution.
        ctx = f"\n\nContext:\n{self.context}" if self.context else ""
        return LLMMessage(
            role="assistant",
            content=f"Plan (revision {self.revision}): {self.summary}\n{body}{ctx}",
        )


class AlternativesEvent(BaseEvent, LLMConvertible):
    """The structured recovery hand-off. Emitted when the agent voluntarily calls
    `ask_user` WITH options — it enumerates 2–3 concrete next-step alternatives,
    those become this event, the loop halts at AWAITING_USER_DECISION, and the
    user picks one (or steers explicitly). The picked option's ToolCall becomes
    the next action. (The harness circuit breaker reaches the same status but with
    a free-form question, no clickable options.)

    Mirrors PlanEvent's shape (gate event + LLMConvertible so the proposed
    options stay in-context for any follow-up turn). `failed_action_id`
    correlates this gate with the action that triggered the recovery."""

    kind: Literal[EventKind.ALTERNATIVES] = EventKind.ALTERNATIVES
    source: EventSource = EventSource.AGENT
    failed_action_id: str  # the original ActionEvent.id that exhausted retries
    summary: str  # what we're choosing between, one sentence
    options: list[AlternativeOption]

    def to_llm_message(self) -> LLMMessage:
        lines = [f"{i}. {o.title} — {o.description}" for i, o in enumerate(self.options, start=1)]
        body = "\n".join(lines)
        return LLMMessage(
            role="assistant",
            content=(
                f"After repeated failures on the prior step, I proposed these "
                f"alternatives: {self.summary}\n{body}"
            ),
        )


class ReportEvent(BaseEvent, LLMConvertible):
    """A finished Deep Research report — the multi-section grounded synthesis
    produced from the per-run corpus. LLMConvertible so a follow-up turn in the
    same conversation has the committed report headers + summary in its View
    (the long body would blow the context window; we render headers only).

    `bounded_by` names the hard-cap that terminated the run, if any: "sources"
    (max_sources hit), "rounds" (max_rounds_per_subq hit on a sub-question),
    "wall_clock" (max_wall_clock_s hit), or "subquestions" (decompose produced
    more than max_subquestions). None means the run completed naturally."""

    kind: Literal[EventKind.REPORT] = EventKind.REPORT
    source: EventSource = EventSource.AGENT
    query: str  # the original user question this report answers
    summary: str  # executive summary (one or two paragraphs, top-of-report)
    sections: list[ReportSection]
    # The cited subset (only passages actually referenced by some section). The UI
    # resolves [[passage_id]] markers against this list to render source cards.
    # Plain dict[str, Any] to keep `core` free of any `retrieval` import — the
    # producer (deep_research engine) populates with Passage.model_dump().
    passages: list[dict[str, Any]] = Field(default_factory=list)
    # Read and retained but not cited. Kept separate so `passages` remains the
    # citation/UI contract while follow-up synthesis can use the full reviewed
    # evidence corpus. Optional-by-default for historical report events.
    reviewed_passages: list[dict[str, Any]] = Field(default_factory=list)
    # The full discovery set (URL, title, snippet, status). All_hits for the
    # All-Searched / Cited tabs at report scale. Same plain-dict reason.
    all_hits: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_count: int = 0  # whole-report total (sum across sections)
    bounded_by: str | None = None  # named cap if hit, else None (natural finish)
    # The depth tier the run used ("quick" / "standard_deep" / "exhaustive"),
    # surfaced for the UI's cost/time honesty + audit. Optional for backward-compat.
    depth_tier: str | None = None

    def to_llm_message(self) -> LLMMessage:
        # Render headers + summary only — the full body is too large for the View
        # and the citations would resolve to ids the model can't look up anyway.
        # This keeps the committed report present in-context for a follow-up turn
        # ("expand section 3") without bloating the window.
        headers = "\n".join(f"## {s.title}" for s in self.sections)
        _trunc = report_truncation(self.bounded_by)
        bound = f"\n\n(Bounded by: {_trunc}.)" if _trunc else ""
        return LLMMessage(
            role="assistant",
            content=f"Research report for: {self.query}\n\n{self.summary}\n\n{headers}{bound}",
        )


class KnowledgeEvent(BaseEvent, LLMConvertible):
    """A scoped best-practice snippet injected into the agent's context (Cluster
    7). `scope` is an optional applicability hint (a task keyword or path glob)
    the View can use to inject only relevant knowledge; `snippet` is the
    guidance. LLMConvertible so it renders into the model context, and pinned
    against condensation so standing guidance survives a long run."""

    kind: Literal[EventKind.KNOWLEDGE] = EventKind.KNOWLEDGE
    source: EventSource = EventSource.SYSTEM
    scope: str = ""
    snippet: str

    def to_llm_message(self) -> LLMMessage:
        scope = f" (applies to: {self.scope})" if self.scope else ""
        return LLMMessage(
            role="user",
            content=f"<knowledge{scope}>\n{self.snippet}\n</knowledge>",
        )


class DatasourceEvent(BaseEvent, LLMConvertible):
    """Durable data-API / schema documentation the agent learned or was given
    (Cluster 7). The long-horizon hazard this fixes: an API contract learned
    mid-run lives only in an Observation's inline text, which condensation later
    compresses into lossy prose → the agent hallucinates an endpoint. A
    DatasourceEvent is condensation-IMMUNE, so the exact contract stays verbatim
    across an arbitrarily long build."""

    kind: Literal[EventKind.DATASOURCE] = EventKind.DATASOURCE
    source: EventSource = EventSource.SYSTEM
    name: str  # the data source / API name
    docs: str  # endpoint shape, auth, params, example response — verbatim

    def to_llm_message(self) -> LLMMessage:
        return LLMMessage(
            role="user",
            content=f'<datasource name="{self.name}">\n{self.docs}\n</datasource>',
        )


class RuntimeConstraintEvent(BaseEvent, LLMConvertible):
    """A host-authored typed runtime constraint, with a usable alternative.

    Why this is typed rather than prose: in the `k460000` diagnostic the process
    backend refused a host-process kill, condensation forgot the refusal span, and
    the model repeated the same forbidden operation.  A refusal delivered only as
    tool-error *text* is ordinary forgettable content.  A constraint carries its own
    identity, so the View can keep exactly one live copy of it near current context
    no matter how many times it is re-observed or how often the log is condensed.

    Field roles:

    ``constraint_key``
        Stable identity.  Re-observing the same prohibition re-emits the same key,
        and only the newest event for a key stays live -- so repeated observations
        never grow context.
    ``scope``
        What the constraint applies to (e.g. the backend it is true of).
    ``guidance``
        Bounded statement of what is not permitted.  Deliberately length-capped: a
        constraint that can grow without limit becomes its own context problem.
    ``alternative``
        The supported way to accomplish the intent.  A prohibition without a usable
        alternative just blocks the model; the recovery path is the point.
    ``capability_generation``
        The host capability generation this was true under.  When the backend or
        its capabilities change generation, constraints from an older generation
        stop being live -- a prohibition must not outlive the configuration that
        justified it.
    ``active``
        False explicitly lifts a constraint for its key.

    **Host authority only.**  ``source`` is fixed to SYSTEM.  Model prose cannot
    create one of these, and a model-authored lookalike in ordinary content has no
    authority because it is not this event type.

    **Transient failures must never produce one.**  A command that failed once and
    may succeed on retry is not a constraint; pinning it would turn a blip into a
    permanent belief.  Only a durable host-enforced prohibition qualifies.
    """

    kind: Literal[EventKind.RUNTIME_CONSTRAINT] = EventKind.RUNTIME_CONSTRAINT
    source: EventSource = EventSource.SYSTEM
    constraint_key: str = Field(min_length=1, max_length=128)
    scope: str = Field(default="", max_length=160)
    guidance: str = Field(min_length=1, max_length=1200)
    alternative: str = Field(default="", max_length=600)
    capability_generation: str = Field(default="", max_length=160)
    active: bool = True

    def to_llm_message(self) -> LLMMessage:
        scope = f' scope="{self.scope}"' if self.scope else ""
        alternative = f"\nInstead: {self.alternative}" if self.alternative else ""
        return LLMMessage(
            role="user",
            content=(
                f'<runtime-constraint key="{self.constraint_key}"{scope}>\n'
                f"{self.guidance}{alternative}\n"
                "</runtime-constraint>"
            ),
        )


class DeliverableEvent(BaseEvent, LLMConvertible):
    """The agent's explicit 'here is the finished thing' handoff (Build). It names
    WHAT was produced and WHERE, so the UI can present a real handoff (open the
    live app / download the files) instead of leaving the user to guess what the
    run made. Distinct from the live preview (which shows work-in-progress): a
    DeliverableEvent is the agent affirming a result is ready.

    `kind`:
      • "app"   — a runnable result the user opens in the live preview (a built
                  site / running dev server selected by the entry file at `path`).
      • "files" — one or more workspace artifacts to download/inspect (`path` is
                  the file or directory).

    LLMConvertible so the agent's own context reflects what it has already handed
    off (it shouldn't re-deliver the same thing); the body stays terse."""

    kind: Literal[EventKind.DELIVERABLE] = EventKind.DELIVERABLE
    source: EventSource = EventSource.AGENT
    title: str  # short human label, e.g. "Landing page" / "Sales report"
    path: str  # workspace-relative path (entry file for apps, file/dir for files)
    artifact_kind: Literal["app", "files"] = "app"
    # Optional canonical URL the deliverable is reachable at (a deploy target, a
    # tunnel, or the live preview). When the agent serves to a known address it
    # passes it on `serve(url=…)`; the UI surfaces an "Open deployed app" link.
    deployment_url: str = ""
    target_id: str = Field(default="", max_length=160)
    delivery_contract: VerificationDeliveryContract | None = None
    verification_contract_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def _target_delivery_identity_is_complete(self) -> DeliverableEvent:
        values = (
            bool(self.target_id),
            self.delivery_contract is not None,
            self.verification_contract_digest is not None,
        )
        if any(values) and not all(values):
            raise ValueError("target-owned deliverable identity must be complete")
        return self

    def to_llm_message(self) -> LLMMessage:
        return LLMMessage(
            role="user",
            content=(
                f'<deliverable kind="{self.artifact_kind}" path="{self.path}">\n'
                f"{self.title}\n</deliverable>"
            ),
        )


__all__ = [
    "AlternativesEvent",
    "DatasourceEvent",
    "DeliverableEvent",
    "KnowledgeEvent",
    "PlanEvent",
    "ReportEvent",
    "RuntimeConstraintEvent",
]
