"""The run's progress events — the UI's only honest source of liveness.

Before this module the backend emitted one ``phase`` before the research loop
and then nothing but ``search``/``observation``, so a front end that wanted to
say anything about progress had to infer it from the conversation status. An
inferred heartbeat is a lie with a spinner on it: it says "working" while a
provider stream is dead and says nothing at all while the loop waits out a
search-engine cooldown.

Every event here is emitted at a boundary the loop actually crosses, and carries
the numbers that boundary really has. In particular ``turn`` carries the SAME
countdown the model reads in its BUDGET line — :func:`turn_position` is the one
place that arithmetic lives, so a UI and a prompt can never disagree about which
turn a run is on.

``model_activity`` (a token heartbeat inside an open provider stream) obeys the
same rule from the other end. The loop cannot see chunks and never invents one:
the numbers come from ``disco.core.llm.stream_progress``, which publishes what
the open socket actually delivered. This module only translates that
observation into the run's event vocabulary — and translates NOTHING when the
call was not one of the run's declared stages.
"""

from __future__ import annotations

import datetime
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Literal

from disco.core.llm.stream_progress import StreamProgress, observe_stream_progress

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

#: A research turn's phases. ``extracting`` is part of the declared contract and
#: is NOT emitted by the loop today: discovery and extraction happen inside one
#: ``RetrievalEngine.retrieve`` call, so the loop has no boundary to honestly
#: report. It stays in the type because the contract declares it; a producer
#: appears when the retrieval engine grows a progress seam, not before.
TurnPhase = Literal["planning", "searching", "extracting", "thinking"]

#: Why the loop is holding. Both are the rate-limit/cooldown class — an outage a
#: wait can fix. A rejected credential or an exhausted quota is never a hold.
HoldReason = Literal["search_pool_cooling", "search_rate_starved"]

#: Which model call a ``model_activity`` heartbeat belongs to. ``brief`` is
#: declared by the contract and has no producer today: the brief arrives inside
#: the FIRST research turn's reply, so that call is honestly reported as
#: ``research_turn``. Same stance as ``extracting`` above — the name stays, a
#: producer appears if the shape ever changes, never before.
#:
#: ``follow_up`` is the answer call a follow-up on a FINISHED report makes. It
#: runs outside the research loop, which is why it was the one long model call
#: with no heartbeat at all — 29–52 s of wire silence against a client that
#: reconnects after 45.
ModelActivityStage = Literal[
    "brief",
    "research_turn",
    "source_reading",
    "draft",
    "review",
    "rework",
    "continuation",
    "follow_up",
]

#: Stop, as the run's own event vocabulary records it. Alone among the names
#: below the SERVER emits this one: the flag is set from outside the run, and
#: the run itself can only report the checkpoint it reaches afterwards. It is
#: declared here so producer, contract suite and TS mirror share one list.
STOP_REQUESTED_ACTION = "stop_requested"

#: What grounding did to a follow-up answer, as one marker before the assistant
#: message. Server-emitted, like ``stop_requested``: the follow-up runs outside
#: the research loop. It exists because a reader — the UI, a spec, the harness —
#: could otherwise only tell an answer from a refusal by matching the refusal's
#: prose, and a length check cannot tell them apart at all.
FOLLOW_UP_GROUNDING_ACTION = "follow_up_grounding"

#: One planned query the host refused before issuing it. It exists because a
#: refusal used to leave no trace anywhere a reader could reach: the reason
#: lived in ``state.trail`` (never persisted) and in the next prompt's HOST
#: FEEDBACK line (cut off by the model-io recorder's head-only cap), so a turn
#: that proposed three queries and issued none showed on the wire as "turn 11
#: searching" followed by nothing. Batch B lost 104 of 490 queries that way,
#: 14 whole turns of them, with no event to say so.
QUERY_REFUSED_ACTION = "query_refused"

#: The action names deep research adds to the ActionEvent stream. The frontend
#: mirrors this list; the harness contract suite compares the two.
RESEARCH_PROGRESS_ACTIONS: tuple[str, ...] = (
    "turn",
    "model_activity",
    "hold",
    "hold_resumed",
    "review",
    "rework",
    "continuation",
    STOP_REQUESTED_ACTION,
    FOLLOW_UP_GROUNDING_ACTION,
    QUERY_REFUSED_ACTION,
)

#: Why one query was refused, as the wire names it — the audit row's own word,
#: because the event is built from that row rather than beside it.
QueryRefusedReason = Literal[
    "repeat", "near_duplicate", "queued", "retry_budget_spent", "exhausted", "empty"
]

#: The run naming the file it saved its whole writer input to, so a reader can
#: replay the writer without researching again (`pool.write_pool`). It is a
#: trace action, not a progress one: nothing about it belongs in a UI
#: heartbeat, and the frontend has no mirror to keep.
RESEARCH_POOL_ACTION = "research_pool"
RECOVERY_ACTION = "research_checkpoint_commit"

#: EVERY action name deep research appends to the conversation log — the
#: progress markers above plus the run's narrative actions. None of them is a
#: tool call a model proposed and an executor ran: they are the server
#: describing its own run, and nothing ever pairs them with a result. A control
#: that closes admitted-but-unpaired actions has to know that, or a killed
#: research run collects one "its outcome is UNKNOWN; re-verify the workspace"
#: error per progress marker — said about a turn counter.
RESEARCH_TRACE_ACTIONS: frozenset[str] = frozenset(
    {
        *RESEARCH_PROGRESS_ACTIONS,
        "brief",
        "phase",
        "search",
        "section_done",
        RESEARCH_POOL_ACTION,
        RECOVERY_ACTION,
        # Pre-v2 replays only; kept so an old conversation kills as cleanly.
        "synthesize_section",
    }
)

#: Streams whose ordinal is still being tracked. A stream that dies mid-flight
#: never reports its close, so the map is bounded rather than trusted to drain.
_MAX_TRACKED_STREAMS = 64


def turn_position(turns_left: int, total_turns: int) -> tuple[int, int]:
    """``(n, of)`` for the turn about to run — the BUDGET line's own numbers.

    The prompt says "N research turns remaining of M"; this says "turn n of M".
    They are the same countdown seen from the two ends, so n is derived from the
    remaining count rather than recomputed from a separate counter.
    """
    return max(1, total_turns - max(0, turns_left) + 1), total_turns


def _resume_at(seconds: float) -> str:
    """A wall-clock ISO instant the UI can count down against."""
    when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=max(0.0, seconds))
    return when.isoformat()


def stop_requested_payload(requested_at: datetime.datetime | None = None) -> dict[str, Any]:
    """What the server truthfully knows the instant Stop is pressed.

    Exactly two facts: that it was pressed, and when. The run's state — the
    step in flight, the sources kept, the turn it is on — is already on the log
    as the events that reported it, each with its own timestamp; restating a
    copy here would give the UI a second source for the same fact that ages
    differently from the first. This event is the marker the run has NOT ended,
    and it must not become a snapshot that quietly contradicts the heartbeat.
    """
    when = requested_at or datetime.datetime.now(datetime.UTC)
    return {"requested_at": when.isoformat()}


def follow_up_grounding_payload(
    claims: Sequence[Mapping[str, Any]], *, refused: bool
) -> dict[str, Any]:
    """The grounding ledger for one follow-up answer — counted, never inferred.

    Every number is a tally of the verdicts ``_verify_claims`` actually
    returned for that answer, so ``supported + weak + removed == statements``
    by construction. ``removed`` is what the retention policy took out, which
    on this path is exactly the ``unsupported`` verdicts; it is a separate
    field rather than arithmetic the reader has to do, because it is the number
    the answer's own removal line quotes.
    """
    verdicts = [str(claim.get("verdict", "unsupported")) for claim in claims]
    return {
        "statements": len(verdicts),
        "supported": verdicts.count("supported"),
        "weak": verdicts.count("weak"),
        "removed": verdicts.count("unsupported"),
        "refused": refused,
    }


async def emit_turn(
    emit: EmitFn,
    *,
    position: tuple[int, int],
    phase: TurnPhase,
    subquestion: str | None = None,
) -> None:
    """One research turn changed phase."""
    payload: dict[str, Any] = {"n": position[0], "of": position[1], "phase": phase}
    if subquestion:
        payload["subquestion"] = subquestion
    await emit("turn", payload)


def model_activity_stage(inspect_stage: str | None) -> ModelActivityStage | None:
    """Which declared stage a provider call belongs to, or None.

    The call sites already declare what they are, in
    ``metadata["inspect_stage"]`` — the label the inspect trace, ``model_io``
    and the acceptance harness bucket every model call by. Reading that is an
    observation; guessing a stage from whatever the loop last emitted would be
    an inference, and inferred liveness is the thing this module removes.

    None means "not one of the run's declared stages" — a query rewrite, a
    grounding pass, anything else riding the same router — and None emits
    NOTHING. The heartbeat speaks only for the calls the contract names.
    """
    if not inspect_stage:
        return None
    # Checked first: `report_draft_continuation` is a continuation, not a draft.
    if inspect_stage.endswith("_continuation"):
        return "continuation"
    if inspect_stage == "source_reading":
        return "source_reading"
    if inspect_stage.startswith("research_turn"):
        return "research_turn"
    if inspect_stage.startswith("follow_up"):
        return "follow_up"
    # The structure re-ask asks for the whole report again; it is a draft call.
    if inspect_stage.startswith(("report_draft", "report_structure")):
        return "draft"
    if inspect_stage.startswith("report_rework"):
        return "rework"
    if inspect_stage.startswith("report_review"):
        return "review"
    return None


async def emit_model_activity(
    emit: EmitFn,
    *,
    stage: ModelActivityStage,
    tokens_streamed: int,
    seconds: float,
    call_ordinal: int,
    reasoning_tokens: int = 0,
    streams: bool = True,
    state: str = "generating",
    details: Mapping[str, Any] | None = None,
) -> None:
    """A provider stream is delivering — this is what it has delivered so far.

    Every number is measured, none is projected: ``tokens_streamed`` is what
    arrived, ``seconds`` is how long the call has been open. There is no timer
    behind this event, so a run that goes quiet emits none and the UI's "no
    signal for N s" is the truth of that moment rather than a contradiction of
    a fabricated one.

    ``streams=False`` marks the one report a BUFFERED transport makes, at call
    start with nothing delivered. The silence after it is the transport's, not
    the model's, and the UI needs the difference to stop reading a working
    driver as a stall.
    """
    payload: dict[str, Any] = {
        "stage": stage,
        "tokens_streamed": tokens_streamed,
        "seconds": round(seconds, 1),
        "call_ordinal": call_ordinal,
    }
    if state != "generating":
        payload["state"] = state
    if details:
        payload.update(
            {
                key: details[key]
                for key in (
                    "source_id",
                    "chunk",
                    "chunks",
                    "attempt",
                    "retry_after_s",
                    "http_status",
                )
                if key in details
            }
        )
    if not streams:
        payload["streams"] = False
    if reasoning_tokens > 0:
        # Only when the provider actually exposed a reasoning channel: on a
        # model whose whole ceiling goes into thinking, this is the difference
        # between "producing nothing" and "producing, none of it visible yet".
        payload["reasoning_tokens"] = reasoning_tokens
    await emit("model_activity", payload)


class _ModelActivityHeartbeat:
    """Run-scoped translator from stream observations to ``model_activity``.

    Owns ``call_ordinal``: the nth model call of THIS run, numbered in the
    order streams open. A router retry opens a new stream and takes the next
    ordinal — it is a further call, and a heartbeat that renumbered it as the
    same one would hide a retry from the only surface that can see it.
    """

    def __init__(self, emit: EmitFn) -> None:
        self._emit = emit
        self._calls = 0
        self._ordinals: dict[int, int] = {}

    async def __call__(self, progress: StreamProgress) -> None:
        stage = model_activity_stage(progress.inspect_stage)
        if stage is None:
            return
        await emit_model_activity(
            self._emit,
            stage=stage,
            tokens_streamed=progress.tokens_streamed,
            seconds=progress.seconds,
            call_ordinal=self._ordinal(progress),
            reasoning_tokens=progress.reasoning_tokens,
            streams=progress.streams,
            state=progress.state,
            details=progress.details,
        )

    def _ordinal(self, progress: StreamProgress) -> int:
        known = self._ordinals.get(progress.stream_id)
        if known is None:
            if len(self._ordinals) >= _MAX_TRACKED_STREAMS:
                # Insertion-ordered, so the front half is the oldest — streams
                # that failed before reporting a close and will never drain.
                for stale in list(self._ordinals)[: _MAX_TRACKED_STREAMS // 2]:
                    del self._ordinals[stale]
            self._calls += 1
            known = self._calls
            self._ordinals[progress.stream_id] = known
        if progress.final:
            self._ordinals.pop(progress.stream_id, None)
        return known


@contextmanager
def model_activity_events(emit: EmitFn) -> Iterator[None]:
    """Emit ``model_activity`` for every declared-stage model call in this run.

    Installed once, around the whole run, for a reason: the thing being
    reported is an open socket several layers below every call site, and the
    ordinal it carries is denominated in the RUN. Wrapping each call site
    instead would put the same context manager in five files and still leave
    the counter homeless.
    """
    with observe_stream_progress(_ModelActivityHeartbeat(emit)):
        yield


#: Audit-row key -> wire key, for the details a refusal class actually has.
#: The two angle fields are renamed because the row namespaces them and the
#: event does not need to: `reason` already says which class they belong to.
_REFUSAL_DETAIL_KEYS: tuple[tuple[str, str], ...] = (
    ("duplicates", "duplicates"),
    ("duplicates_turn", "duplicates_turn"),
    ("exhausted_angle", "angle"),
    ("exhausted_why", "why"),
)


def query_refused_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """One ``query_rejected`` audit row, as the wire sees it.

    Built FROM the row `refusal_trail_rows` already writes, not beside it, so
    the event and the audit can never disagree about why a query was refused.
    Optional keys are present only when the row carried them: a repeat names
    what it duplicates and when, a dead end names the angle and the host's
    reason. Nothing is invented for the ones that have neither.
    """
    payload: dict[str, Any] = {
        "query": str(row.get("query") or ""),
        "reason": str(row.get("rejected") or "empty"),
    }
    for source, key in _REFUSAL_DETAIL_KEYS:
        if (value := row.get(source)) is not None:
            payload[key] = value
    return payload


async def emit_query_refused(emit: EmitFn, row: Mapping[str, Any]) -> None:
    """The host refused one planned query — say so where a reader can see it."""
    await emit(QUERY_REFUSED_ACTION, query_refused_payload(row))


async def emit_hold(
    emit: EmitFn,
    *,
    reason: HoldReason,
    cooling: Mapping[str, float],
    resume_in_s: float,
    sources_retained: int,
    position: tuple[int, int],
    queued_queries: Sequence[str],
) -> None:
    """The loop is waiting out a dead search pool instead of issuing into it."""
    await emit(
        "hold",
        {
            "reason": reason,
            "engines": [
                {"name": name, "resume_at": _resume_at(seconds)}
                for name, seconds in sorted(cooling.items())
            ],
            "resume_at": _resume_at(resume_in_s),
            "sources_retained": sources_retained,
            "turn": {"n": position[0], "of": position[1]},
            "queued_queries": list(queued_queries),
        },
    )


async def emit_hold_resumed(emit: EmitFn, *, waited_s: float, engines_live: Sequence[str]) -> None:
    """The pool came back; the loop is researching again."""
    await emit(
        "hold_resumed",
        {"waited_s": round(waited_s, 1), "engines_live": list(engines_live)},
    )


async def emit_review(emit: EmitFn, k: int, of: int) -> None:
    """Writer review round ``k`` of ``of`` — plus the ``phase`` the UI has
    always seen for it, byte-identical, so nothing existing changes shape."""
    await emit("phase", {"phase": "reviewing", "attempt": k})
    await emit("review", {"k": k, "of": of})


async def emit_rework(emit: EmitFn, k: int, of: int) -> None:
    """Writer rework round ``k`` of ``of``, with its existing ``phase`` event."""
    await emit("phase", {"phase": "writing", "attempt": k + 1})
    await emit("rework", {"k": k, "of": of})


async def emit_continuation(emit: EmitFn, k: int, of: int) -> None:
    """The writer is continuing the same report — round ``k`` of ``of``."""
    await emit("continuation", {"k": k, "of": of})


async def emit_section_done(emit: EmitFn, *, section_id: str, title: str, done: int) -> None:
    """The existing per-section checkpoint pair, unchanged in payload."""
    await emit("phase", {"phase": "synthesize", "section": done})
    await emit("section_done", {"section_id": section_id, "title": title, "done": done})


__all__ = [
    "FOLLOW_UP_GROUNDING_ACTION",
    "QUERY_REFUSED_ACTION",
    "RESEARCH_POOL_ACTION",
    "RESEARCH_PROGRESS_ACTIONS",
    "RESEARCH_TRACE_ACTIONS",
    "STOP_REQUESTED_ACTION",
    "EmitFn",
    "HoldReason",
    "ModelActivityStage",
    "QueryRefusedReason",
    "TurnPhase",
    "emit_continuation",
    "emit_hold",
    "emit_hold_resumed",
    "emit_model_activity",
    "emit_query_refused",
    "emit_review",
    "emit_rework",
    "emit_section_done",
    "emit_turn",
    "follow_up_grounding_payload",
    "model_activity_events",
    "model_activity_stage",
    "query_refused_payload",
    "stop_requested_payload",
    "turn_position",
]
