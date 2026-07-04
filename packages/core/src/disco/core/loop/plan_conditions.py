"""C18 — advisory plan-step done-condition evaluation.

Extracted from engine.py as a stateful collaborator: `PlanStepConditions` holds
a back-ref to its `AgentLoop`, reads the loop's per-(revision, index) predicate
map, and emits advisory notes through the loop's primitives. Sibling calls stay
inside the collaborator; loop state/primitives go through `self._loop`. Bodies
byte-identical to the former AgentLoop methods.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..dod import (
    CommandExitPredicate,
    DoDPredicate,
    FileExistsPredicate,
    HTTPOkPredicate,
)
from ..context import ArtifactMemoryStore, context_mark_resolved, context_write_summary
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
    StatusEvent,
)
from ..selection_edit import is_scoped_edit_directive
from ..view import effective_plan_progress
from .context_live import context_pack_enabled, unresolved_failure_seqs

if TYPE_CHECKING:
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")
_PROGRESS_TOOLS = frozenset({"plan_step", "update_plan_progress"})


@dataclass(frozen=True)
class DictatedContentCondition:
    """A quoted user literal that must survive into the final deliverable.

    This is intentionally derived from the event log instead of persisted as a
    separate mutable store row: replaying USER MessageEvents + PlanEvents
    reconstructs the same requirements after resume/condensation.
    """

    revision: int
    literal: str
    source_event_id: str
    source_seq: int | None = None


# (?<!\w) blocks apostrophe-contractions from OPENING a match ("don't ... 'X'"
# must not capture "t ... " between don't and the real quote); (?!\w) blocks the
# symmetric close-side bridge ("...' s" possessives).
_QUOTED_LITERAL_RE = re.compile(r"(?<!\w)'([^'\n]{2,80})'(?!\w)|(?<!\w)\"([^\"\n]{2,80})\"(?!\w)")
_FILE_LIKE_LITERAL_RE = re.compile(
    r"^(?:[\w.-]+\.(?:html?|css|mjs|cjs|jsx?|tsx?|py|md|json|ya?ml|txt|csv|"
    r"png|jpe?g|gif|webp|svg|pdf|docx?|xlsx?|pptx?)|\.env(?:\.[\w.-]+)?)$",
    re.IGNORECASE,
)
_SHELL_COMMAND_WORDS = frozenset(
    {
        "ag",
        "bun",
        "cat",
        "cd",
        "chmod",
        "chown",
        "cp",
        "curl",
        "deno",
        "docker",
        "echo",
        "git",
        "grep",
        "head",
        "ls",
        "make",
        "mkdir",
        "mv",
        "node",
        "npm",
        "npx",
        "pnpm",
        "pytest",
        "python",
        "python3",
        "rm",
        "ruff",
        "sed",
        "sh",
        "tail",
        "tox",
        "uv",
        "vite",
        "yarn",
    }
)
_SHELL_OPERATOR_RE = re.compile(r"(?:^|\s)(?:&&|\|\||[|;<>])(?:\s|$)|`|\$\(")


def _has_unspaced_slash(text: str) -> bool:
    for idx, ch in enumerate(text):
        if ch != "/":
            continue
        before = text[idx - 1] if idx > 0 else ""
        after = text[idx + 1] if idx + 1 < len(text) else ""
        if not (before.isspace() and after.isspace()):
            return True
    return False


def _looks_like_shell_command(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if s.startswith("$ "):
        return True
    if _SHELL_OPERATOR_RE.search(s):
        return True
    try:
        parts = shlex.split(s)
    except ValueError:
        parts = s.split()
    if not parts:
        return False
    first = parts[0].rsplit("/", 1)[-1].lower()
    return first in _SHELL_COMMAND_WORDS


def _skip_dictated_literal(text: str) -> bool:
    s = text.strip()
    if len(s) != len(text) or not (2 <= len(s) <= 80):
        return True
    if _has_unspaced_slash(s):
        return True
    if _FILE_LIKE_LITERAL_RE.match(s):
        return True
    return _looks_like_shell_command(s)


def extract_dictated_content_literals(text: str) -> list[str]:
    """Extract quoted user-authored content literals from a build instruction.

    Only single/double quoted strings 2..80 chars are considered. Shell-looking
    snippets and path-looking strings are skipped so quoted commands like
    "npm run build" or paths like "src/app.js" do not become content floors.
    De-duplicates per message while preserving first occurrence order.
    """

    out: list[str] = []
    seen: set[str] = set()
    for mt in _QUOTED_LITERAL_RE.finditer(text or ""):
        literal = mt.group(1) if mt.group(1) is not None else mt.group(2)
        if literal is None or _skip_dictated_literal(literal):
            continue
        if literal in seen:
            continue
        seen.add(literal)
        out.append(literal)
    return out


def _scoped_edit_user_messages(events: list[Event]) -> list[MessageEvent]:
    return [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.USER
        and is_scoped_edit_directive(e.message.content or "")
    ]


def _superseded_by_scoped_edit(
    condition: DictatedContentCondition,
    scoped_edits: list[MessageEvent],
) -> bool:
    if condition.source_seq is None:
        return False
    for edit in scoped_edits:
        if edit.seq is None or edit.seq <= condition.source_seq:
            continue
        if condition.literal in (edit.message.content or ""):
            return True
    return False


def _drop_scoped_edit_superseded_conditions(
    conditions: list[DictatedContentCondition],
    events: list[Event],
) -> list[DictatedContentCondition]:
    scoped_edits = _scoped_edit_user_messages(events)
    if not scoped_edits:
        return conditions
    # A label-less directive only supersedes when it still contains the old literal.
    # Otherwise the prior dictated condition remains, by design.
    return [
        c
        for c in conditions
        if not _superseded_by_scoped_edit(c, scoped_edits)
    ]


def dictated_content_conditions_from_events(
    events: list[Event],
) -> list[DictatedContentCondition]:
    """Bind quoted USER literals to the PlanEvent revision that serves them.

    For the initial plan, every USER instruction before the first plan can
    contribute literals. For later revisions, only mutating/revision-intent USER
    messages in the revision window contribute, which avoids turning a quoted
    Q&A phrase into a deliverable requirement for a later change.
    """

    indexed = list(enumerate(events))
    plans = [(idx, e) for idx, e in indexed if isinstance(e, PlanEvent)]
    out: list[DictatedContentCondition] = []
    seen: set[tuple[int, str]] = set()
    prev_plan_idx = -1

    for plan_idx, plan in plans:
        planning_idx: int | None = None
        for idx, e in indexed:
            if idx <= prev_plan_idx or idx > plan_idx:
                continue
            if isinstance(e, StatusEvent) and e.detail == "planning":
                planning_idx = idx
        upper_idx = planning_idx if planning_idx is not None else plan_idx
        users = [
            e
            for idx, e in indexed
            if prev_plan_idx < idx <= upper_idx
            and isinstance(e, MessageEvent)
            and e.source == EventSource.USER
        ]
        if plan.revision > 1:
            from . import signals

            users = [
                e for e in users if signals.is_revision_intent(e.message.content or "")
            ]
        for user in users:
            content = user.message.content or ""
            if is_scoped_edit_directive(content):
                continue
            for literal in extract_dictated_content_literals(content):
                key = (plan.revision, literal)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    DictatedContentCondition(
                        revision=plan.revision,
                        literal=literal,
                        source_event_id=user.id,
                        source_seq=user.seq,
                    )
                )
        prev_plan_idx = plan_idx
    return _drop_scoped_edit_superseded_conditions(out, events)


class PlanStepConditions:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def maybe_emit_plan_step_done_condition_note(self, action: ActionEvent) -> None:
        """If `action` marks one or more plan steps DONE and a step has a stored
        `done_condition` predicate, evaluate it inline and emit a visible pass/fail
        note in the trace. Handles BOTH progress channels:

          * `plan_step(idx, 'done')` — the single step `idx` (small models).
          * `update_plan_progress({steps:[...]})` — the declarative full-state snapshot
            (capable models, the #3 redesign). Fires ONLY for steps that TRANSITION to
            done in THIS action (diffed against the prior effective state) — a snapshot
            re-lists already-done steps every time, so without the transition check it
            would re-spam an advisory for every step on every snapshot.

        ADVISORY ONLY:
          * never blocks the run;
          * never nudges the agent (no <system-reminder>, no auto-continue,
            no speak-back to the model);
          * never duplicates the C1c finish gate (the C18 check uses a
            lightweight inline evaluator; the C1c gate uses the heavy
            fresh-context DoDEvaluator and gates `finish` itself).

        Steps WITHOUT a predicate are a strict no-op (back-compat): no
        lookup, no note, no event. `state="active"` never fires (an active mark is the
        agent saying "I am starting" — there is no work to check yet)."""
        tc = action.tool_call
        if tc is None or tc.tool_name not in ("plan_step", "update_plan_progress"):
            return
        args = tc.arguments or {}
        events = await self._loop._events()
        # Find the latest plan (highest revision; a re-plan supersedes). The
        # `_plan_step_predicates` map is revision-scoped so a stale (revision, idx)
        # never matches a fresh plan.
        latest_plan: PlanEvent | None = None
        for e in events:
            if isinstance(e, PlanEvent):
                if latest_plan is None or e.revision >= latest_plan.revision:
                    latest_plan = e
        if latest_plan is None:
            return  # no plan in scope — the mark is unanchored

        # The step indices THIS action newly marks done.
        if tc.tool_name == "plan_step":
            if str(args.get("state") or "") != "done":
                return
            try:
                raw_index: Any = args.get("index")
                newly_done = [int(raw_index)]
            except (TypeError, ValueError):
                return
        else:  # update_plan_progress — only steps that transitioned to done now
            _, cur = effective_plan_progress(events)
            _, prev = effective_plan_progress([e for e in events if e.id != action.id])
            newly_done = [i for i, st in cur.items() if st == "done" and prev.get(i) != "done"]

        await self._maybe_emit_context_step_done_marks(
            events, latest_plan, newly_done, action
        )

        for idx in newly_done:
            # 1-based step index. Out-of-range = nothing to look up.
            if idx < 1 or idx > len(latest_plan.steps):
                continue
            predicate = self._loop._plan_step_predicates.get((latest_plan.revision, idx))
            if predicate is None:
                # Back-compat: a step with no predicate is the explicit design
                # target (most steps) — silent, no note, no extra event.
                continue
            passed, reason = await self.evaluate_plan_step_predicate(predicate)
            # The note is a <system-reminder>-less MessageEvent from ENVIRONMENT: it is
            # visible in the trace for the human and the LLM sees it next turn as a
            # normal message (NOT a system-reminder, so it does NOT nudge — the model is
            # free to ignore or act). The "advisory" framing makes the no-nudge contract
            # explicit in the trace.
            verdict_word = "met" if passed else "NOT met"
            body = (
                f"[advisory, C18] done-condition for plan step {idx} "
                f"(\"{latest_plan.steps[idx - 1].title}\"): {verdict_word}. "
                f"{reason}"
            )
            await self._loop._emit(
                MessageEvent(
                    source=EventSource.ENVIRONMENT,
                    message=LLMMessage(role="user", content=body),
                    meta={"advisory": "plan_step_done_condition", "passed": passed},
                )
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
        store = ArtifactMemoryStore(sbx)
        failures = unresolved_failure_seqs(events)
        for idx in newly_done:
            if idx < 1 or idx > len(latest_plan.steps):
                continue
            event_range = self._context_step_done_range(events, latest_plan, idx, action)
            if event_range is None:
                continue
            start_seq, end_seq = event_range
            if any(start_seq <= seq <= end_seq for seq in failures):
                continue
            range_id = (
                f"cxr_plan_step_{latest_plan.revision}_{idx}_{start_seq}_{end_seq}"
            )
            if any(
                isinstance(e, ContextResolvedEvent) and e.range_id == range_id
                for e in events
            ):
                continue
            summary = self._context_step_summary(
                events, latest_plan, idx, start_seq, end_seq
            )
            try:
                ref = await store.write_summary(range_id, summary)
            except Exception:  # noqa: BLE001 - context marks are best-effort
                _LOG.warning(
                    "CXT context summary write failed for %s",
                    self._loop.conversation_id,
                    exc_info=True,
                )
                continue
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
                        "step_index": idx,
                    },
                }
            )
            await self._loop._emit(mark)
            await self._loop._emit(
                context_write_summary(range_id, ref.rel_path, summary)
            )

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

    def _context_step_summary(
        self,
        events: list[Event],
        latest_plan: PlanEvent,
        idx: int,
        start_seq: int,
        end_seq: int,
    ) -> str:
        actions = [
            e
            for e in events
            if isinstance(e, ActionEvent)
            and e.seq is not None
            and start_seq <= e.seq <= end_seq
            and e.tool_call.tool_name not in _PROGRESS_TOOLS
        ]
        last = actions[-1] if actions else None
        last_desc = self._tool_summary(last) if last is not None else "none"
        n = len(actions)
        plural = "" if n == 1 else "s"
        title = latest_plan.steps[idx - 1].title
        return f"Step '{title}' completed; {n} tool call{plural}, last: {last_desc}."

    @staticmethod
    def _tool_summary(action: ActionEvent) -> str:
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

    async def evaluate_plan_step_predicate(
        self, predicate: DoDPredicate
    ) -> tuple[bool, str]:
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
        """Check whether `predicate.path` exists, resolved in the SANDBOX's own
        namespace (not the agent-server host cwd).

        B4 root-cause: the container sandbox backend reports `workspace_path is
        None` (the host has NO view of the box FS), yet the agent's files are
        real INSIDE the box. The old code fell through to a literal host
        `Path(path_str).is_file()` check, which looked in the agent-server cwd,
        found nothing, and emitted a FALSE "done-condition NOT met / file
        missing" advisory for files the agent had just written.

        The fix asks the sandbox itself (`await sbx.file_exists(...)`) via duck
        typing — core never imports `tools`; the `executor.sandbox` object is
        injected at runtime and we call a method on it. Each backend resolves in
        its own namespace (host FS for process, inside-the-box for container).

        The path-escape hard-FAIL is preserved whenever a real `workspace_path`
        is known (process/session backend), mirroring the C1b evaluator's
        discipline: a `path` resolving outside the workspace is a hard FAIL, not
        a coincidental pass. For the container backend (`workspace_path is None`)
        the jail is enforced by the backend's own `file_exists` (`_container_path`
        rejects escapes → False).

        The literal-`Path` branch survives ONLY for the sandbox-less /
        fake-executor path (no `file_exists` capability) the tests rely on."""
        from pathlib import Path
        sbx = getattr(self._loop.executor, "sandbox", None)
        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        path_str = predicate.path
        # Path-escape hard-FAIL when a real host-side workspace root is known.
        # (Container backends expose workspace_path=None and rely on their own
        # in-box jail; this branch is the process/session backend's discipline.)
        if workspace:
            try:
                root = Path(workspace).resolve()
                candidate = (root / path_str).resolve()
                root_str = str(root)
                candidate_str = str(candidate)
                # On Windows the resolved strings may differ in case;
                # `Path.resolve()` is case-aware but the prefix check is
                # not, so do a normalized comparison. The agent runs
                # inside a Linux sandbox, so the suffix match is the
                # load-bearing case in practice.
                if not (
                    candidate_str == root_str
                    or candidate_str.startswith(root_str.rstrip("/") + "/")
                ):
                    return (
                        False,
                        f"file_exists: path escapes workspace ({path_str})",
                    )
            except (OSError, ValueError) as exc:
                return (False, f"file_exists({path_str}): resolve error ({exc})")
        # B4 — sandbox-aware existence check. ASK THE SANDBOX (duck-typed; no
        # upward `tools` import) so the path resolves in the box's namespace.
        # This is the load-bearing fix for the container backend's false
        # "missing" advisory.
        if sbx is not None and hasattr(sbx, "file_exists"):
            try:
                exists = await sbx.file_exists(path_str)
            except Exception as exc:  # noqa: BLE001 — advisory; never wedge the loop
                return (False, f"file_exists({path_str}): check error ({exc})")
            if exists:
                return (True, f"file_exists({path_str}): found")
            return (False, f"file_exists({path_str}): missing")
        # No sandbox (or a sandbox without the file_exists capability): check the
        # literal path. This is the sandbox-less / fake-executor path used in
        # tests; the predicate names a literal path, we check the literal path.
        p = Path(path_str)
        if p.exists():
            return (True, f"file_exists({path_str}): found")
        return (False, f"file_exists({path_str}): missing")

    async def check_command_for_plan_step(
        self, predicate: CommandExitPredicate
    ) -> tuple[bool, str]:
        """Run `predicate.cmd` against `predicate.expect_exit` (default 0). Tight
        timeout to keep the loop responsive. Failures (timeout, wrong exit) are
        surfaced with the reason in the note. Advisory-only — never wedges the loop.

        When a sandbox with `exec_shell` is available (container backends that
        report `workspace_path is None`), the command runs INSIDE THE BOX via
        `await sbx.exec_shell(...)`, resolving against the agent's own filesystem.
        This is the load-bearing fix: the prior code used `cwd=None` on the host
        when `workspace_path` was None, evaluating against the agent-server cwd
        instead of the box — a false advisory.

        Timeout caveat: if `ExecResult.timed_out` is True, the result is always
        treated as a FAILURE even when `exit_code == expect_exit` (e.g. 124). A
        host-subprocess timeout cannot pass today; a sandbox timeout must not
        accidentally pass either. The reason is surfaced in the advisory note.

        The host-`subprocess` branch is kept ONLY as the fallback for sandbox-less
        / fake-executor paths (mirroring the `file_exists` fallback design). The
        5 s timeout is preserved for both paths."""
        timeout = 5.0
        sbx = getattr(self._loop.executor, "sandbox", None)

        # Sandbox-aware path: execute the predicate command INSIDE THE BOX so it
        # resolves against the agent's filesystem, not the host's. Duck-typed; no
        # upward `tools` import (core never imports tools).
        if sbx is not None and hasattr(sbx, "exec_shell"):
            try:
                result = await sbx.exec_shell(predicate.cmd, timeout_s=int(timeout))
            except Exception as exc:  # noqa: BLE001 — advisory; never wedge the loop
                return (
                    False,
                    f"command({predicate.cmd!r}): sandbox exec error "
                    f"({type(exc).__name__}: {exc})",
                )
            # Timeout caveat: timed_out=True is always a non-pass, even when
            # exit_code coincidentally matches expect_exit (e.g. 124 from SIGKILL).
            # Surface the reason so the user can see what actually happened.
            if getattr(result, "timed_out", False):
                return (
                    False,
                    f"command({predicate.cmd!r}): timed out in sandbox after {timeout}s "
                    f"(exit_code={result.exit_code}, expect={predicate.expect_exit})",
                )
            if result.exit_code == predicate.expect_exit:
                return (
                    True,
                    f"command({predicate.cmd!r}): exited {result.exit_code} "
                    f"as expected (sandbox)",
                )
            return (
                False,
                f"command({predicate.cmd!r}): exited {result.exit_code}, "
                f"expected {predicate.expect_exit} (sandbox)",
            )

        # Fallback: no sandbox (or a sandbox without exec_shell) — run on the host.
        # This is the sandbox-less / fake-executor path the tests rely on.
        import asyncio
        import subprocess
        from pathlib import Path

        workspace = getattr(sbx, "workspace_path", None) if sbx is not None else None
        cwd = str(Path(workspace).resolve()) if workspace else None
        # 5s is plenty for a per-step done-condition probe — the C1c
        # gate uses the same default. C18 is advisory so we DON'T hang
        # the loop on a stuck command.

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                predicate.cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={"PATH": os.environ.get("PATH", "")},
            )

        started = asyncio.get_event_loop().time()
        try:
            completed = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            return (False, f"command({predicate.cmd!r}): timeout after {timeout}s")
        except Exception as exc:  # noqa: BLE001 — defensive
            return (
                False,
                f"command({predicate.cmd!r}): executor error "
                f"({type(exc).__name__}: {exc})",
            )
        duration = asyncio.get_event_loop().time() - started
        if completed.returncode == predicate.expect_exit:
            return (
                True,
                f"command({predicate.cmd!r}): exited {completed.returncode} "
                f"as expected (in {duration:.2f}s)",
            )
        return (
            False,
            f"command({predicate.cmd!r}): exited {completed.returncode}, "
            f"expected {predicate.expect_exit}",
        )

    async def check_http_for_plan_step(
        self, predicate: HTTPOkPredicate
    ) -> tuple[bool, str]:
        """GET `predicate.url` and compare to `predicate.expect_status`
        (default 200). Tight timeout; failures surface the reason. Does
        NOT enforce the C1b egress allow-list — this is a per-step
        advisory check the agent opted into by attaching the predicate;
        the C1c gate (fresh-context, with egress discipline) is the
        authoritative check."""
        try:
            import httpx
        except ImportError:
            # httpx is in disco-core's deps (we saw it in pyproject.toml),
            # but be defensive in case the test env differs.
            try:
                import urllib.request
                with urllib.request.urlopen(predicate.url, timeout=2.0) as resp:
                    status = int(resp.status)
            except Exception as exc:  # noqa: BLE001 — defensive
                return (False, f"http_ok({predicate.url}): error ({exc})")
        else:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(predicate.url)
                    status = int(resp.status_code)
            except Exception as exc:  # noqa: BLE001 — defensive
                return (False, f"http_ok({predicate.url}): error ({exc})")
        if status == predicate.expect_status:
            return (True, f"http_ok({predicate.url}): status {status} as expected")
        return (
            False,
            f"http_ok({predicate.url}): status {status}, "
            f"expected {predicate.expect_status}",
        )
