"""Execute-and-observe, hard-reset, and the C20 subagent fan-out.

Extracted from engine.py as a stateful collaborator: `Observer` holds a back-ref
to its `AgentLoop` and runs the §4.1 execute→observe contract (incl. the F9
read-dedup gate + sandbox-restart notice), the §8 hard-reset pointer flush, and
the bounded `delegate_explore` fan-out. Bodies are byte-identical to the former
AgentLoop methods with `self.` rewritten to `self._loop.` (sibling calls stay
in-collaborator). None of these methods hold `self._loop._lock` — they run
outside the conversation lock by contract.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from ..events import (
    AgentErrorEvent,
    Event,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    ToolResult,
    find_elided_arg_markers,
)
from ..llm import LLMContextWindowExceeded
from ..view import View
from .dedup import _f9_dedupable_read
from .messages import _workspace_paths_from_events

if TYPE_CHECKING:
    from ..events import ActionEvent
    from .engine import AgentLoop

_LOG = logging.getLogger("disco.loop")

# The helper's input is the driver's `question` + `context` joined into one
# prompt. Bound the size of each so a driver cannot grow the helper's input
# unboundedly within a single segment — the result is folded back into the
# View, which the condenser manages; keeping the helper's prompt bounded
# keeps the post-fold View growth bounded. Symmetric to the search/extract
# length budget (`packages/tools/.../retrieval.py:_EXTRACT_CHAR_BUDGET`).
_FANOUT_INPUT_MAX_CHARS = 4_000


class Observer:
    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    async def collect_pointer_manifest_paths(self, events: list[Event]) -> list[str]:
        """C16 — collect on-disk artifact paths the hard_reset tombstone will
        POINT to (NOT summarize). Three sources, all checked for existence on
        disk so the manifest stays honest (every pointer resolves):

          1. Written deliverables — paths the agent mutated this run (A8's
             mutating-tool set: file_write / file_edit / file_append /
             file_replace_lines / file_insert_lines). Most-recent-first.
          2. Spill logs — `.disco-spill-<uuid>.log` files in the workspace
             (T10's overflow channel; engine.py:1787 already filters them
             from the snapshot because the model has no business re-reading
             them in bulk — but a hard_reset is exactly the time the model
             DOES need to be able to re-read them selectively, so we POINT
             at them instead of summarizing).
          3. `.pmx/MEMORY.md` — standing memory the agent recorded this run
             (C5's MEMORY channel). The view-channel copy is lost on a
             hard filesystem reset; the on-disk copy survives.

        Returns a de-duplicated list, most-recent-first. Paths that don't
        resolve (the sandbox is gone, the file was wiped) are DROPPED — the
        manifest is required to be honest, and a pointer to a missing file
        is worse than no pointer."""
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(p: str | None) -> None:
            if not p or p in seen:
                return
            seen.add(p)
            candidates.append(p)

        # 1. Working-set mutating paths (the agent's deliverable surface).
        mutated, _read = _workspace_paths_from_events(events)
        for p in mutated:
            _add(p)

        sbx = getattr(self._loop.executor, "sandbox", None)
        if sbx is None:
            return candidates

        # 2. Spill logs in the workspace.
        try:
            workspace = getattr(sbx, "workspace_path", None) or ""
            if workspace:
                names = await sbx.list_dir(workspace)
            else:
                # Some backends don't expose workspace_path; fall back to
                # the root. We do not hard-fail on a missing attribute —
                # the manifest degrades gracefully (no spill pointers).
                names = await sbx.list_dir("")  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 — list_dir can raise on a dead backend
            names = []
        for name in names:
            if not isinstance(name, str):
                continue
            bn = os.path.basename(name)
            if bn.startswith(".disco-spill-"):
                _add(name)

        # 3. the standing-memory mirror if present (.disco/, or legacy .pmx/).
        for memory_path in (".disco/MEMORY.md", ".pmx/MEMORY.md"):
            try:
                await sbx.read_file(memory_path)
                _add(memory_path)
                break
            except (FileNotFoundError, NotADirectoryError):
                continue
            except Exception:  # noqa: BLE001 — a dead sandbox just skips the pointer
                break

        return candidates

    async def hard_reset(self, events: list[Event]) -> bool:
        """Forget-and-recover after a context-window error (§8). Returns True
        if a tombstone was appended (progress made).

        C16 — this is a POINTER-ONLY flush (NOT a prose recap). The tombstone's
        summary is a manifest of on-disk artifact paths the model can re-read
        selectively — never a freeform prose summary. The dropped span is
        already too large to re-summarize usefully (it overflowed the
        context window), and a lossy prose recap is worse than pointing at
        the bytes on disk. The soft-condense path (should_condense → condense
        on a token-bound trigger) is byte-unchanged — it still produces a
        prose summary via the summarizer. Only the hard_reset call site
        changes (it now passes reason="hard_reset" + the collected paths)."""
        artifact_paths = await self.collect_pointer_manifest_paths(events)
        tombstone = await self._loop.condenser.condense(
            events,
            View.of(events),
            summarizer=self._loop.summarizer,
            reason="hard_reset",
            artifact_paths=artifact_paths,
        )
        if tombstone is not None:
            await self._loop._emit(tombstone)
            return True
        return False

    async def execute_and_observe(self, action: ActionEvent) -> None:
        """[CONTRACT] Every executed ActionEvent yields exactly one observation
        event (ObservationEvent on success, AgentErrorEvent on failure),
        correlated by action.id.

        NO hidden retries, NO automatic failure escalation. Errors are surfaced
        IMMEDIATELY and IN FULL to the model on its next turn (the Claude Code
        pattern — see the v2 redesign note in this module's header). Recovery
        is a collaboration: the model reasons about the visible error, the user
        watches the trace and can steer at any moment, and the agent has a
        clean `ask_user` tool available when IT decides it needs human input.

        F9 — GATED read-only sliding-window dedup (assist-tier). When
        assist is ON and the current call is a read-only tool that
        EXACTLY repeats a recent read-only call (same tool name + same
        arguments) within the last _F9_WINDOW_SIZE read-only calls, AND
        nothing has invalidated the prior result, short-circuit: emit a
        synthetic ObservationEvent pointing to the prior result instead
        of re-executing. The pointer is short (no content re-render) so
        the model gets the dedup context-savings; the model can always
        re-`file_read` to force a fresh read. Assist OFF → the dedup
        path is never entered; the call falls through to
        executor.execute() exactly as today.

        If the sandbox transparently RECREATED itself during this call (a mid-
        session death the session healed), append an implicit system-reminder
        so the model knows files-on-disk remain but processes/state were lost."""
        # F9 — short-circuit identical read-only calls within the window.
        # Gated on self._assist so capable-model (assist=OFF) runs are
        # byte-identical to today. The dedup fires ONLY when:
        #   1. self._assist is True (the gate — closeable end-to-end);
        #   2. the action is a read-only tool call (per the executor's
        #      readonly_tool_names, falling back to _WORKSPACE_READ_TOOLS);
        #   3. a prior ActionEvent in the last _F9_WINDOW_SIZE read-only
        #      calls has the SAME tool name + SAME arguments;
        #   4. that prior call has a successful observation; AND
        #   5. no mutating tool call has touched the same path between
        #      the prior read and now (stale-read guard).
        # When all five hold we emit a synthetic ObservationEvent
        # carrying the F9 pointer, return WITHOUT calling
        # executor.execute(). The persisted event shape is unchanged
        # (ActionEvent + ObservationEvent pair) — only the observation
        # content differs.
        if self._loop._assist and action.tool_call is not None:
            _f9_events = await self._loop._events()
            _f9_deduped, _f9_prior_id, _f9_pointer = _f9_dedupable_read(
                action.tool_call.tool_name,
                action.tool_call.arguments,
                _f9_events,
                readonly_names=self._loop._readonly_tool_names(),
            )
            if _f9_deduped:
                _LOG.info(
                    "F9 read-dedup: short-circuited %s (call_id=%s, prior_action_id=%s)",
                    action.tool_call.tool_name,
                    action.tool_call.call_id,
                    _f9_prior_id,
                )
                await self._loop._emit(
                    ObservationEvent(
                        tool_result=ToolResult(
                            call_id=action.tool_call.call_id,
                            tool_name=action.tool_call.tool_name,
                            success=True,
                            content=_f9_pointer,
                        ),
                        action_id=action.id,
                    )
                )
                return
        # K1 — elision-marker execution guard. A weak model can copy the
        # `_snip_args` placeholder (rendered into the action history as a context-
        # saving stand-in for content it already wrote) back into a REAL tool
        # argument — e.g. a file_write body. Executing that would overwrite real
        # content with the ~72-byte placeholder (DATA LOSS) and re-feed the marker
        # into the next read (the reproduced 88× read loop). Reject BEFORE
        # execution and tell the model to resend the FULL content from the live
        # CURRENT WORKSPACE snapshot. Catches ALL execution paths (run loop,
        # confirm, verify, finish) because every one funnels through here.
        if action.tool_call is not None:
            _k1_bad = find_elided_arg_markers(action.tool_call.arguments)
            if _k1_bad:
                _LOG.info(
                    "K1 guard: rejected %s — arg(s) %s carry an elision placeholder "
                    "(call_id=%s)",
                    action.tool_call.tool_name,
                    _k1_bad,
                    action.tool_call.call_id,
                )
                await self._loop._emit(
                    AgentErrorEvent(
                        error=(
                            f"Argument(s) {_k1_bad} contain an internal placeholder of "
                            'the form "<N chars … full content …>", not real content. '
                            "That marker is a context-saving stand-in for content you "
                            "ALREADY wrote — it is NOT the content itself, and it was "
                            "NOT executed. Do not copy it into a tool call. Read the "
                            "actual current content from the CURRENT WORKSPACE block "
                            "above (or call file_read) and resend the FULL argument."
                        ),
                        action_id=action.id,
                        tool_call_id=action.tool_call.call_id,
                    )
                )
                return
        sbx = getattr(self._loop.executor, "sandbox", None)
        gen_before = getattr(sbx, "generation", 0) if sbx is not None else 0
        try:
            result = await self._loop.executor.execute(action.tool_call)
        except LLMContextWindowExceeded:
            raise  # handled by view-materialization hard-reset (§8)
        except Exception as e:  # noqa: BLE001 — any tool/exec failure is observable
            await self._loop._emit(
                AgentErrorEvent(
                    error=str(e),
                    action_id=action.id,
                    tool_call_id=action.tool_call.call_id if action.tool_call else None,
                )
            )
            await self.maybe_emit_sandbox_restart(sbx, gen_before)
            return
        if result.success:
            await self._loop._emit(ObservationEvent(tool_result=result, action_id=action.id))
            # C18 — advisory done-condition probe. If this was a
            # `plan_step(idx, 'done')` for a step that had a `done_condition`
            # attached, evaluate the predicate and emit a visible
            # pass/fail note. ADVISORY ONLY: a failure never blocks, never
            # nudges, never duplicates the C1c finish gate. Steps without
            # a predicate are an immediate no-op (back-compat).
            await self._loop._maybe_emit_plan_step_done_condition_note(action)
        else:
            await self._loop._emit(
                AgentErrorEvent(
                    error=result.error or "tool failed",
                    action_id=action.id,
                    tool_call_id=action.tool_call.call_id if action.tool_call else None,
                )
            )
        await self.maybe_emit_sandbox_restart(sbx, gen_before)

    async def maybe_emit_sandbox_restart(self, sbx: object | None, gen_before: int) -> None:
        """If the sandbox's generation grew during the last call AND we already
        had a live instance (gen_before > 0), append an implicit system-reminder
        so the model knows its box was transparently restarted. Cheap, idempotent
        (zero-cost when no restart happened)."""
        if sbx is None or gen_before == 0:
            return
        gen_after = getattr(sbx, "generation", gen_before)
        if gen_after <= gen_before:
            return
        await self._loop._emit(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(
                    role="user",
                    content=(
                        "<system-reminder>\n"
                        "Your sandbox container was restarted mid-session (the prior "
                        "container died and was transparently re-created). Files "
                        "previously written to /workspace remain; any background "
                        "processes or unsaved in-memory state are gone. If you relied "
                        "on running state, re-establish it before continuing.\n"
                        "</system-reminder>"
                    ),
                ),
            )
        )

    async def run_fanout(
        self, args: dict, events: list[Event], *, call_id: str = ""
    ) -> ToolResult:
        """C20 — dispatch the read-only Explore/Plan helper task and return the
        joined result as a `ToolResult` (the caller — the `delegate_explore`
        intercept path — folds it back as a paired `ObservationEvent`).

        The default implementation is a THIN, DETERMINISTIC STUB: it
        serializes the helper's question + context into a short structured
        report and returns it. This is a TEST SEAM — tests override
        `_run_fanout` on the loop instance to control the response. In a
        production wiring, the stub is replaced by a real LLM round-trip
        that offers the helper ONLY the read-only tools
        (file_read / file_list / search / extract) and folds the response
        back the same way. The shape of the return (a `ToolResult`) does
        not change between stub and production; the loop's
        observe-and-continue path is the same either way.

        Why a stub default (vs. a real LLM call here):
          * bounded test — no model required, no flakiness, no
            per-call latency, no cost (a real round-trip would burn
            tokens on every fan-out);
          * the BOUNDED cap + the dispatch+join shape are the load-
            bearing pieces for the C20 acceptance; the helper's
            INTERNAL logic is a separate concern;
          * the production hook is a one-method override; the test
            seam and the production hook are the same surface.

        Returns a `ToolResult(success=True, content=..., structured=...)`
        on a clean dispatch. The `call_id` echoes the loop's call_id so
        the resulting ObservationEvent stays properly paired with the
        proposed ActionEvent the loop just emitted (KV-cache stability,
        same discipline as the remember/serve intercept paths)."""

        # Import ToolResult locally to avoid a circular-import risk at
        # module-load time (the engine module is imported widely; keeping
        # the symbol scoped to the function is the conservative choice).
        from disco.core import ToolResult as _ToolResult

        # Length-bound the question + context (the engine truncates
        # BEFORE dispatching the seam, so a model that overrides
        # _run_fanout also sees a bounded input — the bound is a
        # property of the fan-out, not of this stub). Defensive: the
        # engine's interception path also length-bounds, so by the
        # time we get here the fields are already short; the defensive
        # bound here is a belt-and-suspenders for a direct override
        # that bypasses the engine's path (a unit test, a regression
        # case).
        question = str(args.get("question") or "").strip()
        context = str(args.get("context") or "").strip()
        # Defensive bound (the engine's path already length-bounds; this
        # is a belt-and-suspenders for a direct override).
        _trunc_marker = "…[truncated]"
        if len(question) > _FANOUT_INPUT_MAX_CHARS:
            question = question[: _FANOUT_INPUT_MAX_CHARS - len(_trunc_marker)] + _trunc_marker
        if len(context) > _FANOUT_INPUT_MAX_CHARS:
            context = context[: _FANOUT_INPUT_MAX_CHARS - len(_trunc_marker)] + _trunc_marker
        # The stub's report: a structured, model-readable summary. In a
        # production wiring this is the helper's actual response; here
        # it is a deterministic echo so tests can assert on the shape
        # (and so a caller that does not override the seam still gets
        # a clean, honest "helper ran" report).
        return _ToolResult(
            call_id=call_id,  # echoes the proposed call's call_id (KV-cache pairing)
            tool_name="delegate_explore",
            success=True,
            content=(
                f"[C20 fan-out #{self._loop._fanout_count}/{self._loop._fanout_max}] "
                f"helper dispatched: "
                f"question=\"{question[:80]}{'…' if len(question) > 80 else ''}\""
                + (f" context={len(context)} chars" if context else "")
                + ". (Default stub — override `_run_fanout` for a real subagent.)"
            ),
            structured={
                "fanout_index": self._loop._fanout_count,
                "fanout_max": self._loop._fanout_max,
                "question_chars": len(question),
                "context_chars": len(context),
                "stub": True,
            },
        )
