"""C18 — advisory plan-step done-condition evaluation.

Extracted from engine.py as a stateful collaborator: `PlanStepConditions` holds
a back-ref to its `_LoopFacet`, reads the loop's per-(revision, index) predicate
map, and emits advisory notes through the loop's primitives. Sibling calls stay
inside the collaborator; loop state/primitives go through `self._loop`. Bodies
byte-identical to the former _LoopFacet methods.

Dictated-content condition extraction lives in :mod:`dictated_content_conditions`
and is re-exported here as the compatibility surface.
"""

from __future__ import annotations

import logging
import re  # noqa: F401 — compatibility facade binding
import shlex
from dataclasses import dataclass  # noqa: F401 — compatibility facade binding
from typing import TYPE_CHECKING, Any, Protocol

from ..context import ArtifactMemoryStore, context_mark_resolved, context_write_summary
from ..dod import (
    CommandExitPredicate,
    DoDPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
    predicate_fingerprint,
)
from ..events import (
    ActionEvent,
    AgentErrorEvent,
    ContextResolvedEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,  # noqa: F401 — compatibility facade binding
    VerifierVerdictEvent,
)
from ..selection_edit import is_scoped_edit_directive  # noqa: F401
from ..view import effective_plan_progress
from ..workspace_paths import strip_redundant_workspace_prefix
from .context_live import context_pack_enabled, unresolved_failure_seqs
from .dictated_content_conditions import (  # noqa: F401 — re-exported for back-compat
    _APPLICATION_TITLE_DECLARATION_RE,
    _APPLICATION_TITLE_REPLACEMENT_RE,
    _BUILT_TARGET_TITLE_DECLARATION_RE,
    _DIAGNOSTIC_PROTOCOL_HEADER_RE,
    _DIAGNOSTIC_PROTOCOL_LITERAL_RE,
    _DIAGNOSTIC_PROTOCOL_TASK_RE,
    _DOCUMENT_ARTIFACT_RE,
    _FILE_LIKE_LITERAL_RE,
    _QUOTED_LITERAL_RE,
    _SERVE_METADATA_PREFIX_RE,
    _SHELL_COMMAND_WORDS,
    _SHELL_OPERATOR_RE,
    DictatedContentCondition,
    _dictated_content_literal_document,
    _dictated_content_literal_slot,
    _dictated_content_literals,
    _DictatedContentLiteral,
    _drop_scoped_edit_superseded_conditions,
    _has_unspaced_slash,
    _is_diagnostic_protocol_metadata_literal,
    _is_serve_metadata_literal,
    _looks_like_shell_command,
    _scoped_edit_user_messages,
    _skip_dictated_literal,
    _superseded_by_scoped_edit,
    dictated_content_conditions_from_events,
    extract_dictated_content_literals,
)

if TYPE_CHECKING:
    from .ports import ConversationModePort, LoopEventPort, PlanLifecyclePort, ToolExecutionPort

    class _LoopFacet(
        ConversationModePort,
        LoopEventPort,
        PlanLifecyclePort,
        ToolExecutionPort,
        Protocol,
    ):
        """The loop capability this module uses: conversation mode, the event log, the plan
        lifecycle, tool execution.
        """


_LOG = logging.getLogger("disco.loop")
_PROGRESS_TOOLS = frozenset({"plan_step", "update_plan_progress"})


def _context_tool_summary(action: ActionEvent) -> str:
    name = action.tool_call.tool_name
    args = action.tool_call.arguments or {}
    for key in ("path", "rel_path", "file"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return f"{name} {value[:120]}"
    command = args.get("command")
    if isinstance(command, str) and command:
        try:
            head = shlex.join(shlex.split(command)[:3])
        except ValueError:
            head = command
        return f"{name} {head[:120]}"
    return name


def _context_evidence_line(event: Event) -> str | None:
    if isinstance(event, ActionEvent) and event.tool_call.tool_name not in _PROGRESS_TOOLS:
        return f"- seq {event.seq} OBSERVED action: {_context_tool_summary(event)}"
    if isinstance(event, ObservationEvent):
        state = "success" if event.tool_result.success else "failure"
        exit_code = (event.tool_result.structured or {}).get("exit_code")
        exit_note = f" exit {exit_code}" if isinstance(exit_code, int) else ""
        return (
            f"- seq {event.seq} OBSERVED result: {event.tool_result.tool_name} {state}{exit_note}"
        )
    if isinstance(event, VerifierVerdictEvent):
        state = "PASS" if event.verified else "FAIL"
        return f"- seq {event.seq} HOST VERIFICATION {state}: {event.artifact_path or 'artifact'}"
    return None


def _context_ranged_events(events: list[Event], start_seq: int, end_seq: int) -> list[Event]:
    return [
        event for event in events if event.seq is not None and start_seq <= event.seq <= end_seq
    ]


def _context_chronology(ranged: list[Event]) -> list[str]:
    lines = [line for event in ranged if (line := _context_evidence_line(event))]
    return lines or ["- No non-progress tool or host-verifier evidence in this range."]


def _context_verification(ranged: list[Event]) -> str:
    verdicts = [event for event in ranged if isinstance(event, VerifierVerdictEvent)]
    if not verdicts:
        return (
            "HOST VERIFICATION: none. Tool results above are observed facts; "
            "the step marks are the agent's."
        )
    return "HOST VERIFICATION: " + ", ".join(
        "PASS" if event.verified else "FAIL" for event in verdicts
    )


def _context_step_summary(
    events: list[Event],
    latest_plan: PlanEvent,
    indices: list[int],
    start_seq: int,
    end_seq: int,
) -> str:
    ranged = _context_ranged_events(events, start_seq, end_seq)
    actions = [
        event
        for event in ranged
        if isinstance(event, ActionEvent) and event.tool_call.tool_name not in _PROGRESS_TOOLS
    ]
    chronology = _context_chronology(ranged)
    verification = _context_verification(ranged)
    claims = "; ".join(f"{idx} '{latest_plan.steps[idx - 1].title}'" for idx in indices)
    last_desc = _context_tool_summary(actions[-1]) if actions else "none"
    plural = "" if len(actions) == 1 else "s"
    return "\n".join(
        [
            f"CLAIMED done: plan step{'' if len(indices) == 1 else 's'} {claims}.",
            f"OBSERVED: {len(actions)} tool call{plural}; last: {last_desc}.",
            f"Chronology (seq {start_seq}-{end_seq}):",
            *chronology,
            verification,
        ]
    )


def _latest_plan_from_events(events: list[Event]) -> PlanEvent | None:
    """Find the latest (highest revision) PlanEvent in events."""
    latest_plan: PlanEvent | None = None
    for e in events:
        if isinstance(e, PlanEvent):
            if latest_plan is None or e.revision >= latest_plan.revision:
                latest_plan = e
    return latest_plan


def _newly_done_steps(action: ActionEvent, events: list[Event]) -> list[int] | None:
    """Return the step indices this action newly marks done, or None if N/A."""
    tc = action.tool_call
    if tc is None:
        return None
    args = tc.arguments or {}
    if tc.tool_name == "plan_step":
        if str(args.get("state") or "") != "done":
            return None
        try:
            raw_index: Any = args.get("index")
            return [int(raw_index)]
        except (TypeError, ValueError):
            return None
    # update_plan_progress — only steps that transitioned to done now
    _, cur = effective_plan_progress(events)
    _, prev = effective_plan_progress([e for e in events if e.id != action.id])
    return [i for i, st in cur.items() if st == "done" and prev.get(i) != "done"]


def _resolve_persisted_action(events: list[Event], action: ActionEvent) -> ActionEvent | None:
    """Return the persisted copy of action (with seq), or None."""
    if action.seq is not None:
        return action
    return next(
        (
            e
            for e in events
            if isinstance(e, ActionEvent) and e.id == action.id and e.seq is not None
        ),
        None,
    )


def _emit_step_done_note(
    loop: _LoopFacet, idx: int, latest_plan: PlanEvent, passed: bool, reason: str
) -> MessageEvent:
    verdict_word = "met" if passed else "NOT met"
    body = (
        f"[advisory, C18] done-condition for plan step {idx} "
        f'("{latest_plan.steps[idx - 1].title}"): {verdict_word}. '
        f"{reason}"
    )
    return MessageEvent(
        source=EventSource.ENVIRONMENT,
        message=LLMMessage(role="user", content=body),
        meta={
            "advisory": "plan_step_done_condition",
            "passed": passed,
            # This is durable trace/UI telemetry, not a new instruction.  Projecting
            # it into the driver as a role=user message made an agent rewrite a
            # functionally complete app solely to satisfy filenames it had proposed
            # during planning, despite C18 having no completion authority.
            "model_visibility": "trace_only",
        },
    )


class PlanStepConditions:
    def __init__(self, loop: _LoopFacet) -> None:
        self._loop = loop

    async def maybe_emit_plan_step_done_condition_note(self, action: ActionEvent) -> None:
        """If `action` marks one or more plan steps DONE and a step has a stored
        `done_condition` predicate, evaluate it inline and emit a visible pass/fail
        note in the trace/UI (but not the driving model's view). Handles BOTH
        progress channels:

          * `plan_step(idx, 'done')` — the single step `idx` (small models).
          * `update_plan_progress({steps:[...]})` — the declarative full-state snapshot
            (capable models, the #3 redesign). Fires ONLY for steps that TRANSITION to
            done in THIS action (diffed against the prior effective state) — a snapshot
            re-lists already-done steps every time, so without the transition check it
            would re-spam an advisory for every step on every snapshot.

        ADVISORY ONLY:
          * never blocks the run;
          * never nudges the agent (the durable MessageEvent is explicitly
            trace-only and excluded from model projection);
          * never duplicates the C1c finish gate (the C18 check uses a
            lightweight inline evaluator; the C1c gate uses the heavy
            fresh-context DoDEvaluator and gates `finish` itself).

        Steps WITHOUT a predicate are a strict no-op (back-compat): no
        lookup, no note, no event. `state="active"` never fires (an active mark is the
        agent saying "I am starting" — there is no work to check yet)."""
        tc = action.tool_call
        if tc is None or tc.tool_name not in ("plan_step", "update_plan_progress"):
            return
        events = await self._loop._events()
        latest_plan = _latest_plan_from_events(events)
        if latest_plan is None:
            return
        newly_done = _newly_done_steps(action, events)
        if newly_done is None:
            return
        await self._maybe_emit_context_step_done_marks(events, latest_plan, newly_done, action)
        for idx in newly_done:
            if idx < 1 or idx > len(latest_plan.steps):
                continue
            predicate = self._loop._plan_step_predicates.get((latest_plan.revision, idx))
            if predicate is None:
                continue
            passed, reason = await self.evaluate_plan_step_predicate(predicate)
            await self._loop._emit(
                _emit_step_done_note(self._loop, idx, latest_plan, passed, reason)
            )

    async def _maybe_emit_context_step_done_marks(
        self,
        events: list[Event],
        latest_plan: PlanEvent,
        newly_done: list[int],
        action: ActionEvent,
    ) -> None:
        if not context_pack_enabled() or not newly_done:
            return
        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            return
        stored = _resolve_persisted_action(events, action)
        if stored is None:
            return
        action = stored
        store = ArtifactMemoryStore(sbx)
        failures = unresolved_failure_seqs(events)
        grouped: dict[tuple[int, int], list[int]] = {}
        for idx in newly_done:
            if idx < 1 or idx > len(latest_plan.steps):
                continue
            event_range = self._context_step_done_range(events, latest_plan, idx, action)
            if event_range is None:
                continue
            start_seq, end_seq = event_range
            if any(start_seq <= seq <= end_seq for seq in failures):
                continue
            grouped.setdefault(event_range, []).append(idx)
        for (start_seq, end_seq), indices in sorted(grouped.items()):
            await self._emit_context_step_group(
                events, latest_plan, indices, start_seq, end_seq, store
            )

    async def _emit_context_step_group(
        self,
        events: list[Event],
        latest_plan: PlanEvent,
        indices: list[int],
        start_seq: int,
        end_seq: int,
        store: ArtifactMemoryStore,
    ) -> None:
        ordered = sorted(set(indices))
        index_key = "-".join(str(idx) for idx in ordered)
        noun = "step" if len(ordered) == 1 else "steps"
        range_id = f"cxr_plan_{noun}_{latest_plan.revision}_{index_key}_{start_seq}_{end_seq}"
        if any(
            isinstance(e, ContextResolvedEvent)
            and e.reason == "plan_step_done"
            and e.range_id == range_id
            for e in events
        ):
            return
        summary = _context_step_summary(events, latest_plan, ordered, start_seq, end_seq)
        try:
            ref = await store.write_summary(range_id, summary)
        except Exception:  # noqa: BLE001 - context marks are best-effort
            _LOG.warning(
                "CXT context summary write failed for %s",
                self._loop.conversation_id,
                exc_info=True,
            )
            return
        fingerprints: dict[str, str | None] = {}
        for idx in ordered:
            predicate = latest_plan.steps[idx - 1].done_condition
            fingerprints[str(idx)] = (
                predicate_fingerprint(predicate) if predicate is not None else None
            )
        mark = context_mark_resolved(
            start_seq,
            end_seq,
            reason="plan_step_done",
            range_id=range_id,
        ).model_copy(
            update={
                "source": EventSource.SYSTEM,
                "summary_ref_path": ref.rel_path,
                "meta": {
                    "plan_revision": latest_plan.revision,
                    "step_indices": ordered,
                    "predicate_fingerprints": fingerprints,
                    "step_index": ordered[0] if len(ordered) == 1 else None,
                    "predicate_fingerprint": (
                        fingerprints[str(ordered[0])] if len(ordered) == 1 else None
                    ),
                },
            }
        )
        await self._loop._emit(mark)
        await self._loop._emit(context_write_summary(range_id, ref.rel_path, summary))

    def _context_step_done_range(
        self,
        events: list[Event],
        latest_plan: PlanEvent,
        idx: int,
        action: ActionEvent,
    ) -> tuple[int, int] | None:
        end_seq = self._action_pair_end_seq(events, action)
        if end_seq is None:
            return None
        start_seq = self._step_start_seq(events, latest_plan, idx, action)
        if start_seq is None:
            return None
        return (min(start_seq, end_seq), end_seq)

    @staticmethod
    def _action_pair_end_seq(events: list[Event], action: ActionEvent) -> int | None:
        end_seq = action.seq
        call_id = action.tool_call.call_id if action.tool_call is not None else None
        for e in events:
            if isinstance(e, ObservationEvent):
                if e.action_id == action.id or e.tool_result.call_id == call_id:
                    if e.seq is not None:
                        end_seq = max(end_seq or e.seq, e.seq)
            elif isinstance(e, AgentErrorEvent):
                if e.action_id == action.id or e.tool_call_id == call_id:
                    if e.seq is not None:
                        end_seq = max(end_seq or e.seq, e.seq)
        return end_seq

    def _step_start_seq(
        self,
        events: list[Event],
        latest_plan: PlanEvent,
        idx: int,
        action: ActionEvent,
    ) -> int | None:
        action_seq = action.seq
        if action_seq is None:
            return None
        boundary_seq = self._step_boundary_seq(events, latest_plan, idx, action)
        after_seq = boundary_seq if boundary_seq is not None else latest_plan.seq
        for e in events:
            if not isinstance(e, ActionEvent) or e.seq is None:
                continue
            if e.seq > action_seq:
                break
            if after_seq is not None and e.seq <= after_seq:
                continue
            if e.tool_call.tool_name not in _PROGRESS_TOOLS:
                return e.seq
        if boundary_seq is not None:
            return boundary_seq
        return action_seq

    def _step_boundary_seq(
        self,
        events: list[Event],
        latest_plan: PlanEvent,
        idx: int,
        action: ActionEvent,
    ) -> int | None:
        boundary_seq: int | None = None
        for e in events:
            if e.id == action.id:
                break
            if not isinstance(e, ActionEvent):
                continue
            if latest_plan.seq is not None and e.seq is not None and e.seq < latest_plan.seq:
                continue
            for mark_idx, state in self._progress_updates(e, len(latest_plan.steps)):
                if e.seq is None:
                    continue
                if mark_idx == idx and state == "active":
                    boundary_seq = e.seq
                elif mark_idx == idx - 1 and state == "done" and boundary_seq is None:
                    boundary_seq = e.seq
        return boundary_seq

    @staticmethod
    def _progress_updates(action: ActionEvent, total_steps: int) -> list[tuple[int, str]]:
        name = action.tool_call.tool_name
        args = action.tool_call.arguments or {}
        updates: list[tuple[int, str]] = []
        if name == "plan_step":
            raw = [(args.get("index"), args.get("state"))]
        elif name == "update_plan_progress":
            raw = [
                (s.get("index"), s.get("state"))
                for s in args.get("steps") or []
                if isinstance(s, dict)
            ]
        else:
            return []
        for raw_idx, raw_state in raw:
            try:
                idx = int(raw_idx)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if 1 <= idx <= total_steps:
                updates.append((idx, str(raw_state)))
        return updates

    async def evaluate_plan_step_predicate(self, predicate: DoDPredicate) -> tuple[bool, str]:
        """Lightweight inline evaluation of a single DoDPredicate for the
        C18 advisory note. The three kinds reuse the
        `disco.core.dod.DoDPredicate` discriminated union:

          * `file_exists(path)`: resolved against the executor's sandbox
            workspace root (so the predicate talks about agent-visible
            files, not harness-private paths), with a path-escape check
            mirroring the C1b evaluator's discipline (a `path` that
            resolves outside the workspace is a hard FAIL — the
            predicate is not silently passed by an out-of-scope match).
          * `command(cmd, expect_exit)`: a tight-timeout subprocess run
            against the workspace root. Deny-list failures and timeouts
            count as a non-pass with the reason surfaced.
          * `http_ok(url, expect_status)`: a short-timeout GET against
            the URL; a non-matching status is a non-pass.

        Returns `(passed, reason)`. The reason is a one-line human-
        readable summary the C18 note emits in the trace; on failure it
        names WHY the predicate did not pass (file missing, command
        exited N, http status 5xx, etc.) so the user can see the truth
        of the check, not just a boolean.

        Distinct from the C1c gate's `DoDEvaluator`: this is a single-
        predicate inline check that runs in the same context as the rest
        of the loop, not a fresh-context judge. The C1c gate is the
        authoritative DoD check at finish time; C18 is the
        per-step advisory trail."""
        if isinstance(predicate, FileExistsPredicate):
            return await self.check_file_exists_for_plan_step(predicate)
        if isinstance(predicate, CommandExitPredicate):
            return await self.check_command_for_plan_step(predicate)
        if isinstance(predicate, HTTPOkPredicate):
            return await self.check_http_for_plan_step(predicate)
        # Defensive: the union is closed (three kinds) and
        # `predicate_from_obj` rejects unknown kinds at submit_plan
        # time. A future kind would land here as a fail-loud non-pass
        # rather than a silent pass.
        return (False, f"unsupported predicate kind: {type(predicate).__name__}")

    async def check_file_exists_for_plan_step(
        self, predicate: FileExistsPredicate
    ) -> tuple[bool, str]:
        sbx = getattr(self._loop.executor, "sandbox", None)
        return await _check_file_exists(predicate, sbx)

    async def check_command_for_plan_step(
        self, predicate: CommandExitPredicate
    ) -> tuple[bool, str]:
        sbx = getattr(self._loop.executor, "sandbox", None)
        return await _check_command(predicate, sbx)

    async def check_http_for_plan_step(self, predicate: HTTPOkPredicate) -> tuple[bool, str]:
        return await _check_http(predicate)


async def _check_file_exists(
    predicate: FileExistsPredicate,
    sbx: Any,
) -> tuple[bool, str]:
    """Check whether `predicate.path` exists, resolved in the SANDBOX's own namespace."""
    from pathlib import Path

    workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
    path_str = predicate.path
    normalized_path = strip_redundant_workspace_prefix(path_str)
    if workspace:
        try:
            root = Path(workspace).resolve()
            candidate = (root / normalized_path).resolve()
            root_str = str(root)
            candidate_str = str(candidate)
            if not (
                candidate_str == root_str or candidate_str.startswith(root_str.rstrip("/") + "/")
            ):
                return (False, f"file_exists: path escapes workspace ({path_str})")
        except (OSError, ValueError) as exc:
            return (False, f"file_exists({path_str}): resolve error ({exc})")
    if sbx is not None and hasattr(sbx, "file_exists"):
        try:
            exists = await sbx.file_exists(normalized_path)
        except Exception as exc:  # noqa: BLE001 — advisory; never wedge the loop
            return (False, f"file_exists({path_str}): check error ({exc})")
        if exists:
            return (True, f"file_exists({path_str}): found")
        return (False, f"file_exists({path_str}): missing")
    # No sandbox file surface → UN-EVALUATABLE, never a host filesystem read.
    # `predicate.path` is model-authored, and on this branch there is no
    # workspace to clamp it to, so `Path(path_str).exists()` answered a
    # model-chosen absolute path against the agent-server's own filesystem —
    # the wrong machine's evidence, and an existence oracle for it. Fail closed.
    return (
        False,
        f"file_exists({path_str}): not evaluated — no sandbox available to check it in "
        f"(host filesystem access is not permitted)",
    )


async def _check_command(
    predicate: CommandExitPredicate,
    sbx: Any,
) -> tuple[bool, str]:
    """Run `predicate.cmd` against `predicate.expect_exit` (default 0)."""
    timeout = 5.0
    from ..security.analyzers import hard_deny_reason

    _deny = hard_deny_reason(predicate.cmd)
    if _deny is not None:
        return (False, f"command({predicate.cmd!r}): hard-denied ({_deny})")
    if sbx is not None and hasattr(sbx, "exec_shell"):
        try:
            result = await sbx.exec_shell(predicate.cmd, timeout_s=int(timeout))
        except Exception as exc:  # noqa: BLE001 — advisory; never wedge the loop
            return (
                False,
                f"command({predicate.cmd!r}): sandbox exec error ({type(exc).__name__}: {exc})",
            )
        if getattr(result, "timed_out", False):
            return (
                False,
                f"command({predicate.cmd!r}): timed out in sandbox after {timeout}s "
                f"(exit_code={result.exit_code}, expect={predicate.expect_exit})",
            )
        if result.exit_code == predicate.expect_exit:
            return (
                True,
                f"command({predicate.cmd!r}): exited {result.exit_code} as expected (sandbox)",
            )
        return (
            False,
            f"command({predicate.cmd!r}): exited {result.exit_code}, "
            f"expected {predicate.expect_exit} (sandbox)",
        )
    # No sandbox exec surface → the check is UN-EVALUATABLE, never a host
    # subprocess. `predicate.cmd` is a model-authored string harvested verbatim
    # from `submit_plan` / `propose_plan_update` (shape-validated only), and the
    # destruction deny-list above is not an arbitrary-code-execution control.
    # Running it here would run it in the agent-server's own process, cwd and
    # PATH. This mirrors `_maybe_emit_context_step_done_marks`, which already
    # bails out when `sbx is None`. Fail closed: non-pass, and say why.
    return (
        False,
        f"command({predicate.cmd!r}): not evaluated — no sandbox available to run it in "
        f"(host execution is not permitted)",
    )


async def _check_http(predicate: HTTPOkPredicate) -> tuple[bool, str]:
    """GET `predicate.url` and compare to `predicate.expect_status`."""
    try:
        from disco.core.host_egress import guarded_get

        resp = await guarded_get(predicate.url, timeout_s=2.0)
        status = int(resp.status_code)
    except Exception as exc:  # noqa: BLE001 — defensive
        return (False, f"http_ok({predicate.url}): error ({exc})")
    if status == predicate.expect_status:
        return (True, f"http_ok({predicate.url}): status {status} as expected")
    return (
        False,
        f"http_ok({predicate.url}): status {status}, expected {predicate.expect_status}",
    )
