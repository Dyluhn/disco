"""Shared constants and pure helpers for Loop turn control."""

import ipaddress
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..events import (
    ActionEvent,
    AdmittedVerificationContract,
    BuildPlatformAdmissionEvent,
    DeliverableEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
    current_build_platform_admission,
    event_matches_current_workspace_intent,
)
from ..verification import requires_structured_browser_runtime
from . import signals
from .boundaries import AgentStep
from .control import Disp
from .ordinals import ordinal
from .turn_control_serve_refusals import (
    serve_target_shape_guidance as _serve_target_shape_guidance,
    questions_v2_attempts_since_last_plan as _questions_v2_attempts_since_last_plan,
    serve_argument_refusal,
    serve_spend_escalation,
)
from .stuck import (
    F6_FILE_MUTATING_TOOLS,
    VerifierFailureNoProgress,
)

if TYPE_CHECKING:
    pass

_LOG = logging.getLogger("disco.loop")

# plan_step-spam guard (issue C). Soft nudge first, hard halt well below the
# 15-31× spam observed live; thresholds are conservative so a weak model doing a
# small legitimate bookkeeping burst is never penalized.
_BOOKKEEPING_STREAK_NUDGE_AT = 3
_BOOKKEEPING_STREAK_HALT_AT = 6
# Slack added to the active plan's step count when sizing the HALT cap, so a
# model that legitimately marks each plan step done (one `plan_step` per step)
# plus a couple of over-corrections/verifications isn't penalized.
_BOOKKEEPING_PLAN_SLACK = 2

# C8 (T11): bound the autonomous `propose_plan_update` loop. When the proposed
# plan's steps are byte-identical to the immediately-prior plan for >= this many
# consecutive auto-approved revisions, feed the existing bookkeeping-stuck valve.
_PROPOSE_PLAN_UPDATE_REPEAT_CAP = 3
_STUCK_ESCAPE_BLOCKED_TOOLS_BY_REASON: dict[str, frozenset[str]] = {
    "repeated_unchanged_file_read": frozenset({"file_read"}),
    "redundant_read_after_churn_nudge": frozenset({"file_read"}),
    "redundant_read_coverage": frozenset({"file_read"}),
}
_IDENTICAL_PLAN_NUDGE_DIAGNOSTIC = "identical_plan_nudge"
_IDENTICAL_PLAN_NUDGE_TEXT = (
    "This revision is IDENTICAL to the already-approved plan — proposing it "
    "again does nothing. The plan is current: continue executing its steps, "
    "mark progress with update_plan_progress, or finish. One more identical "
    "proposal will halt the run."
)

_SERVE_HANDOFF_DIAGNOSTIC = "serve_handoff_recorded"
_SERVE_DUPLICATE_DIAGNOSTIC = "serve_duplicate_ignored"


def _serve_next_move(events: list[Event]) -> str:
    """The ONE next move after a handoff, PROJECTED from the loop's own state.

    GROUNDED FEEDBACK constraint 3 (the projection rule), and this seam is the
    owner's named canonical counter-example. Both serve guidances used to end:

        "If verification and plan work are complete, call `finish` now;
         otherwise perform the remaining work, then call `finish`."

    That sentence hands the done-determination back to the agent — the one
    determination the agent is worst placed to make and the platform already
    holds. `_plan_done_and_verified` sits forty lines below and answers it from
    the durable log; `effective_plan_progress` is the single source of truth for
    per-step completion. The message asked the model to re-derive, from memory,
    a fact the host had computed. The 97903 cell took that fork into its F47 red.

    So the fork is gone. This returns exactly one move, drawn from the SAME
    projection the loop grades with, or says plainly that nothing is outstanding.
    Message and enforcement cannot drift because they read one source.
    """
    from ..view import effective_plan_progress

    plan, states = effective_plan_progress(events)
    if plan is not None and plan.steps:
        pending = [
            index for index in range(1, len(plan.steps) + 1) if states.get(index) != "done"
        ]
        if pending:
            index = pending[0]
            title = (plan.steps[index - 1].title or "").strip()
            named = f" ({title})" if title else ""
            return (
                f"Next move: plan step {index}{named} is not marked done. Do that "
                "step, then call `finish`."
            )
    if not _last_verify_web_app_passed(events):
        if plan is None or not plan.steps:
            return (
                "Next move: call `finish`. The record shows no plan step "
                "outstanding and no passing `verify_web_app` on file — if this "
                "deliverable needs one, run it first."
            )
        return (
            "Next move: every plan step is marked done, but there is no passing "
            "`verify_web_app` result on record. Run that verification, then call "
            "`finish`."
        )
    return (
        "Next move: call `finish`. Every plan step is marked done and the latest "
        "`verify_web_app` passed."
    )


def _serve_handoff_guidance(repeats: int, events: list[Event]) -> str:
    """Handoff-recorded reminder. Derived at the moment of use (constraint 1),
    and repetition-aware since 2026-08-07f (constraint 4, F51).

    This function used to take only ``events``, and that was the defect. Its
    sibling `_serve_duplicate_guidance` sits TEN LINES BELOW, takes ``repeats``,
    and escalates — written by this campaign against the 2026-07-27
    counted-promotion failure in which the agent "received a BYTE-IDENTICAL
    reminder each time". The two seams answer adjacent questions and only one of
    them had learned the lesson: at 2026-08-07d this surface fired **3× byte
    identically in `pilota/001` and 2× in `canary/000`**.

    Constraint 1 was never the problem here — the body IS derived, and
    `_serve_next_move` projects the one outstanding move from the plan and
    verification record. Constraints 1 and 4 are independent: in a repeat loop
    the state a derived surface renders has not changed *by construction*, so
    "derived" and "byte-identical every fire" are perfectly compatible. Only a
    count breaks that tie, and the count is ledger-derived
    (`_prior_diagnostic_count`), not an instance counter, so it survives a restart
    or condensation exactly as the run's own evidence does.

    A repeat here means the agent handed off a DISTINCT artifact again rather than
    finishing — so the escalation names the count and the real cost, which is that
    `serve` is not counted as work and each attempt spends a turn.
    """
    if repeats <= 1:
        return (
            "<system-reminder>\n"
            "Handoff recorded. `serve` does not complete the run. Do not serve this "
            f"artifact again.\n{_serve_next_move(events)}\n"
            "</system-reminder>"
        )
    return (
        "<system-reminder>\n"
        f"Handoff recorded — this is the {ordinal(repeats)} handoff of this run, and none "
        "of them completes it. `serve` hands an artifact over; it is not counted as "
        "work, so each further handoff spends one turn toward the no-progress limit "
        f"that ENDS this run.\n{_serve_next_move(events)}\n"
        "Do not serve again before doing that.\n"
        "</system-reminder>"
    )


def _serve_duplicate_guidance(repeats: int, events: list[Event]) -> str:
    """Duplicate-serve guidance that ESCALATES instead of repeating verbatim.

    Counted-promotion failure 2026-07-27 (`p4_ff_static_continue` seed 600002,
    ACTIONLESS_THRASH). The agent handed off, then called `serve` twice more and
    received a BYTE-IDENTICAL reminder each time. `serve` is non-productive, so
    each attempt was an actionless turn, and the third tripped the actionless
    valve — on a run that went on to FINISH successfully.

    The first reminder was correct and actionable, so this is not a broken
    contract. But repeating the same sentence gives a model that already
    mis-read it nothing new to act on, and never says that the next repeat ends
    the run. Naming the remaining move and the cost is information the host
    already has; withholding it is what turns one misread into a terminal.

    Deliberately NOT a threshold change: the actionless cap is untouched, and a
    genuinely stuck agent still lands in the same valve at the same count.

    2026-08-06z: the escalation used to offer "exactly two moves" — finish IF
    complete, or do remaining work — which is the same delegated fork the
    first-fire text carried, restated more urgently. Both branches now project
    the ONE move `_serve_next_move` derives from the plan/verification record.
    An escalation that repeats the platform's uncertainty is not an escalation.
    """
    if repeats <= 1:
        return (
            "<system-reminder>\n"
            "This artifact was already handed off, so the duplicate `serve` call "
            f"was ignored. Do not serve it again.\n{_serve_next_move(events)}\n"
            "</system-reminder>"
        )
    return (
        "<system-reminder>\n"
        f"STOP — `serve` has now been ignored as a duplicate {repeats} times. "
        "The handoff is already recorded; serving cannot change anything and is "
        "not counted as work, so each attempt spends one turn toward the "
        f"no-progress limit that ENDS this run.\n{_serve_next_move(events)}\n"
        "Do not call `serve` again.\n"
        "</system-reminder>"
    )


_SERVE_TARGET_SHAPE_DIAGNOSTIC = "serve_target_shape_refused"
# D2: the reserved AlternativesEvent option id for "Continue anyway" — the bypass
# the user can always pick at the circuit-breaker gate to reset the failure streak
# and let the agent keep going. The frontend renders it as a distinct button;
# pick_alternative special-cases it (reset streak + resume) rather than running a tool.
_CONTINUE_OPTION_ID = "__continue__"


def _prior_plan_and_productive_action_between(
    events: list[Event], new_plan: PlanEvent
) -> tuple[PlanEvent | None, bool]:
    current_idx: int | None = None
    for idx, event in enumerate(events):
        if isinstance(event, PlanEvent) and event.id == new_plan.id:
            current_idx = idx
    if current_idx is None:
        return None, False

    prior_idx: int | None = None
    prior_plan: PlanEvent | None = None
    for idx in range(current_idx - 1, -1, -1):
        event = events[idx]
        if isinstance(event, PlanEvent):
            prior_idx = idx
            prior_plan = event
            break
    if prior_idx is None or prior_plan is None:
        return None, False

    for event in events[prior_idx + 1 : current_idx]:
        if (
            isinstance(event, ActionEvent)
            and event.tool_call is not None
            and event.tool_call.tool_name not in signals._BOOKKEEPING_TOOLS
        ):
            return prior_plan, True
    return prior_plan, False


def _rewrite_directive_marker_active(events: list[Event], path: str) -> bool:
    marker_detail = f"rewrite_directive:{path}"
    marker_index: int | None = None
    for i, event in enumerate(events):
        if isinstance(event, StatusEvent) and event.detail == marker_detail:
            marker_index = i
    if marker_index is None:
        return False

    matching_actions: set[str] = set()
    for event in events[marker_index + 1 :]:
        if isinstance(event, ActionEvent):
            tc = event.tool_call
            if tc.tool_name in F6_FILE_MUTATING_TOOLS and tc.arguments.get("path") == path:
                matching_actions.add(event.id)
        elif (
            isinstance(event, ObservationEvent)
            and event.action_id in matching_actions
            and event.tool_result.success
        ):
            return False
    return True


def _coerce_choice_label(opt: object, _depth: int = 0) -> list[str]:
    """Normalize ONE raw `clarify` choice-option into zero or more clean label
    strings. Defensive against the model's shape drift — exactly the discipline
    `plans.py:alternatives_from_args` applies to `ask_user` options, which the
    clarify handler historically lacked.

    Observed live malformations (build `clarify`, real models) that the naive
    `str(o)` mangled into garbage or empties:
      * a `{"id": "...", "question"/"label": "..."}` object  → `str(dict)`
        rendered the whole Python-repr as one radio label.
      * a NESTED list `["A", ["B", "C"]]`                    → `str(list)`
        rendered the whole repr as one radio label.
      * empty placeholders `["", "", ""]`                     → blank radios
        ("empty bubble cards" with nothing to pick).

    This returns FLAT, human-readable, non-empty labels; dicts contribute their
    best text field; nested lists are flattened; empties are dropped."""
    if _depth > 4:
        return []
    if isinstance(opt, str):
        s = opt.strip()
        return [s] if s else []
    if isinstance(opt, bool):
        return [str(opt)]
    if isinstance(opt, (int, float)):
        return [str(opt)]
    if isinstance(opt, dict):
        return _coerce_mapping_choice_label(opt)
    if isinstance(opt, (list, tuple)):
        return _coerce_choice_sequence(opt, _depth)
    return []


def _coerce_mapping_choice_label(opt: dict[object, object]) -> list[str]:
    # Prefer a human-readable field; the model reuses the multi-question
    # item shape ({id, question}) and the ask_user option shape
    # ({title, description}) interchangeably inside `options`.
    for key in ("label", "title", "text", "value", "name", "question", "option"):
        value = opt.get(key)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
    # No known text key — fall back to the single string value if unambiguous.
    values = [value.strip() for value in opt.values() if isinstance(value, str) and value.strip()]
    return [values[0]] if len(values) == 1 else []


def _coerce_choice_sequence(opt: list[object] | tuple[object, ...], depth: int) -> list[str]:
    labels: list[str] = []
    for item in opt:
        labels.extend(_coerce_choice_label(item, depth + 1))
    return labels


def _normalize_clarify_options(raw: object) -> list[str]:
    """Flatten a raw `options` value into clean, de-duplicated label strings.
    Empty / unrecoverable options are dropped — a `choice` left with too few
    real options is downgraded to free text by the caller (no blank radios)."""
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for opt in raw:
        for label in _coerce_choice_label(opt):
            if label not in seen:
                seen.add(label)
                out.append(label)
    return out


_QUESTIONS_V2_REQUIRED_OPTIONS = (
    "Explore a few options",
    "Decide for me",
    "Other",
)


def _normalize_questions_v2_options(raw: object) -> list[str]:
    options = _normalize_clarify_options(raw)
    seen = {o.casefold() for o in options}
    for required in _QUESTIONS_V2_REQUIRED_OPTIONS:
        if required.casefold() not in seen:
            options.append(required)
            seen.add(required.casefold())
    return options


def _questions_v2_used_since_last_plan(events: list[Event]) -> bool:
    from ..events import QuestionsV2Event

    for event in reversed(events):
        if isinstance(event, PlanEvent):
            return False
        if isinstance(event, QuestionsV2Event):
            return True
    return False


# WALK-19 — the corrective reminder injected when the no-progress breaker first
# trips. Failure-independent: the model's edits are "succeeding" but the app is
# unchanged, so the message reframes toward a hypothesis + a DIFFERENT outcome.
_NO_PROGRESS_REMINDER = (
    "<system-reminder>\n"
    "Your recent edits keep applying successfully but the app/verify outcome "
    "has NOT changed across several different attempts — you are likely editing "
    "code that does not affect what you're observing (wrong file, wrong layer, a "
    "cached build, or the symptom has a different root cause). STOP making more "
    "varied edits. In the SAME turn as your next tool call, state a one-line "
    "hypothesis for WHY the outcome is unchanged — the call itself must be a "
    "DIFFERENT diagnostic step (read the actual served output / console errors, "
    "check the build is rebuilding, or inspect a different layer), not another "
    "edit. Stating the hypothesis without a tool call does not count. If the "
    "same error keeps recurring, use the `search`/`extract` tools to look up "
    "the exact error or API before retrying, and decide whether the blocker is "
    "your CODE or the ENVIRONMENT (sandbox / network / a missing tool) — if "
    "it's the environment, work AROUND it with a different path. If you "
    "cannot make the observed result change, call `finish` and state what is "
    "blocked.\n"
    "</system-reminder>"
)
_VERIFIER_NO_PROGRESS_DETAIL = "verifier_no_progress"
_VERIFIER_EVIDENCE_INVALID_DETAIL = "verifier_evidence_invalid"


def _verifier_no_progress_marker_seq(
    events: list[Event], finding: VerifierFailureNoProgress
) -> int | None:
    """Semantic marker for this exact verifier/fingerprint streak.

    ``Event.meta`` is advisory and may be dropped during persistence/projection,
    so marker identity and ordering live entirely in ``detail`` + ``seq``.
    """
    for event in reversed(events):
        if isinstance(event, MessageEvent) and event.source == EventSource.USER:
            return None
        if not (
            isinstance(event, StatusEvent)
            and event.detail == finding.marker_detail
            and event.seq is not None
            and event.seq > finding.streak_start_seq
        ):
            continue
        return event.seq
    return None


async def _serve_path_missing(loop, path: str) -> bool:  # noqa: ANN001 — AgentLoop, avoids import cycle
    """CD-TOOLS-5 OUTPUT-TRUTH: True iff the deliverable path is VERIFIABLY absent in the
    workspace. Fail-OPEN (return False) when there's no sandbox or the existence check errors —
    serve is the handoff softguard, not a hard gate, and the verify gate is the real proof; an
    unverifiable check must never block a legitimate handoff."""
    sbx = getattr(loop.executor, "sandbox", None)
    if sbx is None:
        return False
    try:
        return not await sbx.file_exists(path)
    except Exception:  # noqa: BLE001 — unverifiable → don't block the handoff
        return False


def _normalize_serve_path(path: str) -> str:
    from disco.core.workspace_paths import strip_redundant_workspace_prefix

    return strip_redundant_workspace_prefix(path.strip())


def _serve_path_is_workspace_root(path: str) -> bool:
    return path in {"", ".", "./"}


def _serve_root_refusal() -> str:
    return (
        "serve refused: serve takes the entry FILE path, not the workspace root. "
        "For a site pass 'index.html' (or your entry file). If index.html exists "
        "at the root it will be served from there."
    )


async def _coerce_serve_entry_path(loop, raw_path: str) -> tuple[str, bool, bool]:  # noqa: ANN001
    """Return (normalized_path, root_like, coerced_to_index)."""
    path = _normalize_serve_path(raw_path)
    if not _serve_path_is_workspace_root(path):
        return path, False, False
    if await _serve_path_verified_present(loop, "index.html"):
        _LOG.info(
            "serve path %r points at the workspace root; auto-coerced to index.html",
            raw_path,
        )
        return "index.html", True, True
    return path, True, False


async def _serve_path_verified_present(loop, path: str) -> bool:  # noqa: ANN001 — AgentLoop, avoids import cycle
    """Fail-CLOSED twin of _serve_path_missing: True only when the sandbox
    POSITIVELY confirms the path exists. The fresh-session gate's exception
    for already-built work must not open on an unverifiable check."""
    sbx = getattr(loop.executor, "sandbox", None)
    if sbx is None:
        return False
    try:
        return bool(await sbx.file_exists(path))
    except Exception:  # noqa: BLE001 — unverifiable → no exception granted
        return False


@dataclass(frozen=True)
class _ServeRequest:
    title: str
    path: str
    kind: str
    url: str


_SERVE_ARGUMENT_DIAGNOSTIC = "serve_argument_refused"
_SERVE_FRESH_DIAGNOSTIC = "serve_fresh_session_refused"


async def _refuse_serve_argument(  # noqa: ANN001
    loop, step: AgentStep, events: list[Event], defect: str, detail: str = ""
) -> Disp:
    """Emit one rendered serve-argument refusal through the shared valve."""
    return await loop._valve.refuse_fresh_session(
        step,
        serve_argument_refusal(
            _prior_diagnostic_count(events, _SERVE_ARGUMENT_DIAGNOSTIC), step, defect, detail
        ),
        diagnostic=_SERVE_ARGUMENT_DIAGNOSTIC,
    )


async def _fresh_serve_refusal(  # noqa: ANN001
    loop,
    step: AgentStep,
    events: list[Event],
) -> Disp | None:
    assert step.tool_call is not None
    if signals.actions_since_last_resume(events) != 0:
        return None
    raw_path = str((step.tool_call.arguments or {}).get("path") or "").strip()
    path, _, _ = await _coerce_serve_entry_path(loop, raw_path)
    if path and await _serve_path_verified_present(loop, path):
        return None
    # Constraint 4: names the entry it could not find and the ONE move that
    # changes the answer, and escalates rather than restating itself.
    missing = (
        f"`{path}` does not exist in the workspace yet"
        if path
        else "no entry path was resolvable from the arguments"
    )
    return await loop._valve.refuse_fresh_session(
        step,
        serve_spend_escalation(
            _prior_diagnostic_count(events, _SERVE_FRESH_DIAGNOSTIC),
            "before any real work exists in this session",
            "serve refused: no real work has happened yet in this session — the "
            f"sandbox is fresh and nothing is running, and {missing}.\n"
            f"{_serve_next_move(events)}",
        ),
        diagnostic=_SERVE_FRESH_DIAGNOSTIC,
    )


async def _validate_serve_request(  # noqa: ANN001
    loop,
    step: AgentStep,
    events: list[Event],
) -> _ServeRequest | Disp:
    assert step.tool_call is not None
    fresh_refusal = await _fresh_serve_refusal(loop, step, events)
    if fresh_refusal is not None:
        return fresh_refusal

    arguments = step.tool_call.arguments or {}
    if not arguments:
        return await _refuse_serve_argument(loop, step, events, "no_arguments")
    title = str(arguments.get("title") or "").strip()
    raw_path = str(arguments.get("path") or "").strip()
    path, root_like, coerced_to_index = await _coerce_serve_entry_path(loop, raw_path)
    kind = str(arguments.get("kind") or "app").strip()
    url = _canonical_deployment_url(str(arguments.get("url") or ""))
    if not title:
        return await _refuse_serve_argument(loop, step, events, "title_missing")
    if not raw_path:
        return await _refuse_serve_argument(loop, step, events, "path_missing")
    if kind not in ("app", "files"):
        return await _refuse_serve_argument(loop, step, events, "kind_invalid", kind)
    if root_like and not coerced_to_index:
        return await loop._valve.refuse_fresh_session(
            step, _serve_root_refusal(), diagnostic=_SERVE_ARGUMENT_DIAGNOSTIC
        )
    if not path:
        return await _refuse_serve_argument(loop, step, events, "path_unresolvable", raw_path)
    return _ServeRequest(title=title, path=path, kind=kind, url=url)


def _expected_serve_kind(admission: BuildPlatformAdmissionEvent | None) -> str | None:
    admitted_contract = admission.verification_contract if admission is not None else None
    return (
        "app"
        if admitted_contract is not None and admitted_contract.delivery.mode == "interactive"
        else "files"
        if admitted_contract is not None
        else "app"
        if admission is not None
        and requires_structured_browser_runtime(admission.verification_claims)
        else None
    )


def _duplicate_serve(
    events: list[Event],
    request: _ServeRequest,
    admitted_contract: AdmittedVerificationContract | None,
) -> bool:
    latest_handoff = next(
        (event for event in reversed(events) if isinstance(event, DeliverableEvent)),
        None,
    )
    return (
        latest_handoff is not None
        and event_matches_current_workspace_intent(events, latest_handoff)
        and latest_handoff.path == request.path
        and latest_handoff.artifact_kind == request.kind
        and latest_handoff.verification_contract_digest
        == (admitted_contract.digest if admitted_contract is not None else None)
    )


def _prior_diagnostic_count(events: list[Event], diagnostic: str) -> int:
    """How many times this refusal has already been emitted, plus this one.

    Read from the DURABLE log rather than an instance counter so an escalation
    survives a restart or condensation exactly as the run's own evidence does.
    Shared by the duplicate-serve and shape-refusal escalations, which had
    byte-identical copies of this walk.
    """
    return 1 + sum(
        1
        for event in events
        if isinstance(event, MessageEvent) and event.meta.get("diagnostic") == diagnostic
    )


async def _record_serve_handoff(  # noqa: ANN001
    loop,
    step: AgentStep,
    events: list[Event],
    request: _ServeRequest,
) -> Disp:
    admission = current_build_platform_admission(events)
    admitted_contract = admission.verification_contract if admission is not None else None
    expected_kind = _expected_serve_kind(admission)
    if expected_kind is not None and request.kind != expected_kind:
        loop._invisible_steps += 1
        repeats = _prior_diagnostic_count(events, _SERVE_TARGET_SHAPE_DIAGNOSTIC)
        await loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=_serve_target_shape_guidance(expected_kind, request.kind, repeats),
                ),
                meta={
                    "diagnostic": _SERVE_TARGET_SHAPE_DIAGNOSTIC,
                    "expected_kind": expected_kind,
                    "offered_kind": request.kind,
                    "shape_refusals": repeats,
                },
            )
        )
        return await loop._post_noop_valve()

    if _duplicate_serve(events, request, admitted_contract):
        _LOG.debug("Skipping duplicate deliverable: %s (%s)", request.path, request.kind)
        loop._invisible_steps += 1
        repeats = _prior_diagnostic_count(events, _SERVE_DUPLICATE_DIAGNOSTIC)
        await loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user", content=_serve_duplicate_guidance(repeats, events)
                ),
                meta={"diagnostic": _SERVE_DUPLICATE_DIAGNOSTIC, "duplicate_serves": repeats},
            )
        )
    elif await _serve_path_missing(loop, request.path):
        return await loop._valve.refuse_fresh_session(
            step,
            f"serve refused: checked normalized deliverable path {request.path!r}, "
            "but it does not exist in the workspace. Serve takes the entry "
            "FILE path; for a site that is usually 'index.html'. If the file "
            "is elsewhere, pass that workspace-relative entry path; otherwise "
            "build or write the intended entry file first, then serve that file.",
        )
    else:
        # A first, distinct handoff is real control-plane progress: it resolves
        # the missing/incorrect delivery shape even though `serve` is a virtual
        # tool with no ActionEvent. Clear prior log-invisible refusal debt here.
        # The DeliverableEvent itself still counts in consecutive_noops, so
        # distinct serve spam remains capped at three and duplicate spam remains
        # capped after two duplicates.
        loop._invisible_steps = 0
        handoffs = _prior_diagnostic_count(events, _SERVE_HANDOFF_DIAGNOSTIC)
        await loop._emit(
            DeliverableEvent(
                source=EventSource.AGENT,
                title=request.title,
                path=request.path,
                artifact_kind=request.kind,  # type: ignore[arg-type]
                deployment_url=request.url,
                target_id=admitted_contract.target_id if admitted_contract is not None else "",
                delivery_contract=(
                    admitted_contract.delivery if admitted_contract is not None else None
                ),
                verification_contract_digest=(
                    admitted_contract.digest if admitted_contract is not None else None
                ),
            )
        )
        await loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user", content=_serve_handoff_guidance(handoffs, events)
                ),
                meta={"diagnostic": _SERVE_HANDOFF_DIAGNOSTIC, "serve_handoffs": handoffs},
            )
        )
    if await loop._post_noop_valve() is Disp.HALT:
        return Disp.HALT
    return Disp.CONTINUE


async def _handle_serve(loop, step: AgentStep, events: list[Event]) -> Disp:  # noqa: ANN001
    """Validate and record one finished-artifact handoff."""
    request = await _validate_serve_request(loop, step, events)
    if isinstance(request, Disp):
        return request
    return await _record_serve_handoff(loop, step, events, request)


_BLOCKED_LANDING_META_KEY = "blocked_landing"
_BLOCKED_DETAIL_PREFIX = "blocked:"
_ACTIONLESS_AUTO_RESUME_MARKER = "AUTO-RESUME-ONCE(actionless)"
_ACTIONLESS_AUTO_RESUME_SEGMENT_CAP = 3


def _last_verify_web_app_passed(events: list[Event]) -> bool:
    """True iff the most recent verify_web_app observation is a PASS."""
    for e in reversed(events):
        if not (isinstance(e, ObservationEvent) and e.tool_result.tool_name == "verify_web_app"):
            continue
        structured = e.tool_result.structured
        if isinstance(structured, dict) and "passed" in structured:
            return e.tool_result.success and structured.get("passed") is True
        content = str(e.tool_result.content or "")
        if "VERIFY_WEB_APP:" in content:
            return e.tool_result.success and "VERIFY_WEB_APP: PASS" in content
        return False
    return False


def _plan_done_and_verified(events: list[Event]) -> bool:
    """True when every effective plan step is done AND the most recent
    verify_web_app observation PASSED — the done-not-stuck discriminator."""
    from ..view import effective_plan_progress

    plan, states = effective_plan_progress(events)
    if plan is None or not plan.steps:
        return False
    if any(states.get(i) != "done" for i in range(1, len(plan.steps) + 1)):
        return False
    if _last_verify_web_app_passed(events):
        return True
    return False


def _ensure_question(content: str, fallback: str) -> str:
    """Keep a blocked landing actionable even when the model omits a question."""
    text = content.strip()
    if not text:
        return fallback
    if "?" in text:
        return text
    if len(text.split()) < 8:
        return fallback
    return f"{text}\n\nWhat should I do next?"


def _no_progress_finish_hinted(events: list[Event], marker_seq: int) -> bool:
    return any(
        isinstance(e, StatusEvent)
        and e.detail == "no_progress_finish_hint"
        and e.seq is not None
        and e.seq > marker_seq
        for e in events
    )


def _no_progress_marker_seq(events: list[Event]) -> int | None:
    """Seq of the most recent `no_progress` marker since the last USER message,
    else None — mirrors `signals.stuck_escape_seq`. One nudge per user turn;
    a second no-progress trip after the model acted on the nudge halts."""
    for e in reversed(events):
        if isinstance(e, StatusEvent) and e.detail == "no_progress":
            return e.seq
        if isinstance(e, MessageEvent) and e.source == EventSource.USER:
            return None
    return None


def _canonical_deployment_url(raw: str) -> str:
    """Return a served `deployment_url` ONLY when it is reachable from OUTSIDE the
    sandbox; otherwise return "".

    F3 (live build stress-test): when the agent serves a site it reports the
    address it bound INSIDE the sandbox (observed: ``http://127.0.0.1:8000/``).
    That loopback/private address is reachable only from inside the sandbox —
    never from the host browser, the operator, or the verifier. Worse, :8000 also
    happens to be the Disco agent-server's own port, so following the URL verbatim
    hits the wrong server. Surfacing it as the deliverable's "Deployed" URL is a
    false affordance.

    The honest, host-reachable address is the preview-proxy route
    (``/conversations/{cid}/preview-app/``), which every consumer already falls
    back to when there is no canonical URL (frontend DeliverablePanel/PreviewPane,
    operator `view`, the verify runner's URL-less app branch). So we keep the URL
    only for an absolute http(s) URL whose host is publicly reachable (a real
    deploy target / tunnel hostname or a global IP); a loopback / unspecified /
    private / link-local / reserved host is dropped to "".
    """
    url = (raw or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname
    if host.lower() in {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }:
        return ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A real DNS hostname (deploy target / tunnel) — keep it.
        return url
    # A bare IP literal: keep only a globally-routable (public) address.
    return url if ip.is_global else ""
