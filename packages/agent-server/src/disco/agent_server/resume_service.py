"""Resume reconstruction + trailing-degeneracy condensation — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The mode-agnostic
resume path (DC-05b/c, DEFECT-4) — the trailing-degeneracy detector, the
resume-context reconstruction (synthetic observations + the environment
reality block), and the `resume_conversation` orchestrator — moves out of
runtime.py into a `ResumeService` collaborator constructed once in
`ConversationRuntime`.

`ResumeService` reaches the runtime's live state (`_store`, `_executors`,
`_cancel_flags`, `kick`, `sessions_snapshot`, `_surface_of`,
`_project_store_now`, `get_upload_names`) via a back-reference. All three
methods stay reachable on `ConversationRuntime` as one-line delegators
because the resume test-suite calls each directly on the runtime
(`rt._condense_trailing_degeneracy`, `rt._reconstruct_resume_context`,
`rt.resume_conversation`).
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    ConversationStatus,
    EventSource,
    KnowledgeEvent,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    StatusEvent,
    ToolResult,
    agent_view_consistent_events,
)
from disco.core.loop.engine import _BOOKKEEPING_TOOLS
from disco.core.view import effective_plan_progress
from disco.tools.projects import StorageStatus

_RESUME_DRAIN_TIMEOUT_S = 10.0
_RESUME_CANCEL_GRACE_S = 0.25


class ResumeService:
    """Resume reconstruction logic; live runtime state via the back-ref."""

    def __init__(self, rt: Any) -> None:
        self._rt = rt

    def _detach_finishing_task_locked(
        self,
        conversation_id: str,
        observed_task: asyncio.Task[Any] | None,
        observed_generation: int | None,
    ) -> tuple[bool, asyncio.Task[Any] | None]:
        """Detach only the exact stopped run observed by a fenced resume.

        The boolean distinguishes an already-finished observed task from an
        identity race. No await occurs between the identity checks and the
        registry pop, so this method can never pop a newer run task.
        """

        if not self._rt._workspace.lock(conversation_id).locked():
            raise RuntimeError("resume task detach requires the workspace fence")
        if self._rt._run_generation.get(conversation_id) != observed_generation:
            return False, None
        current = self._rt._tasks.get(conversation_id)
        if current is not observed_task:
            # The done callback may have removed the exact observed task while
            # the caller awaited durable state. That is completion, not takeover.
            if observed_task is not None and observed_task.done() and current is None:
                self._rt._workspace.clear_run_claim(conversation_id)
                return True, None
            return False, None
        if current is None:
            return True, None
        self._rt._tasks.pop(conversation_id, None)
        self._rt._workspace.clear_run_claim(conversation_id)
        return True, None if current.done() else current

    async def _drain_finishing_task(self, task: asyncio.Task[Any] | None) -> bool:
        """Boundedly drain one already-detached cooperative-stop task.

        The task object—not a conversation id—is the authority. If another
        ingress registers a newer task while this await yields, ``wait_for`` can
        only complete or cancel this exact old task and cannot inspect, pop, or
        cancel the replacement.
        """

        if task is None or task.done():
            return True
        try:
            # Shield makes the timeout itself bounded: ``wait_for(task)`` waits
            # indefinitely for a cancellation-resistant task to acknowledge
            # cancellation. On timeout, explicitly cancel only this old task and
            # give cleanup one separately-bounded grace window.
            await asyncio.wait_for(asyncio.shield(task), _RESUME_DRAIN_TIMEOUT_S)
        except TimeoutError:
            task.cancel()
            done, _pending = await asyncio.wait(
                {task},
                timeout=_RESUME_CANCEL_GRACE_S,
            )
            return task in done
        except asyncio.CancelledError:
            # Cancellation of the resume request is not success. Propagate it,
            # while ensuring the task we detached cannot continue unregistered.
            task.cancel()
            raise
        except Exception:
            # The runtime task's own done callback owns crash terminalization.
            return True
        return True

    def _condense_trailing_degeneracy(self, events: list) -> CondensationEvent | None:
        """Pure-on-the-event-list detector for trailing degenerate segments.
        No model call (call-site: resume_conversation).

        DC-05c Design:
        1. Detection: scan tail backwards to first real Action (non-bookkeeping)
           or USER Message. Count agent messages, duplicate knowledge, plan revisions.
        2. Degenerate iff: segment >= 6 AND zero real actions AND (>=3 agent messages
           OR any knowledge fact >= 3 times).
        3. Pinning: the FIRST instance of any duplicated knowledge fact stays
           OUTSIDE the span.
        """
        # Scan backwards to define the segment boundary
        # "stopping at the first real ActionEvent (non-bookkeeping tool) or USER MessageEvent"
        segment_events = []
        for e in reversed(events):
            if isinstance(e, MessageEvent) and e.source == EventSource.USER:
                break
            if isinstance(e, ActionEvent):
                # A real (non-bookkeeping) action ends the degenerate segment.
                # Same set the engine's noop counter exempts — single source
                # of truth, NOT a local copy.
                if e.tool_call.tool_name not in _BOOKKEEPING_TOOLS:
                    break
            segment_events.append(e)

        if len(segment_events) < 6:
            return None

        segment_events.reverse()

        # count degeneracy signals
        agent_prose_count = 0
        knowledge_counts: dict[tuple[str, str], int] = {}  # (scope, hash) -> count
        plan_revision_count = 0

        for e in segment_events:
            if isinstance(e, MessageEvent) and e.message.role == "assistant":
                agent_prose_count += 1
            elif isinstance(e, KnowledgeEvent):
                snippet_hash = hashlib.sha256(e.snippet.strip().encode()).hexdigest()
                key = (e.scope, snippet_hash)
                knowledge_counts[key] = knowledge_counts.get(key, 0) + 1
            elif isinstance(e, PlanEvent):
                plan_revision_count += 1

        is_degenerate = agent_prose_count >= 3 or any(
            count >= 3 for count in knowledge_counts.values()
        )

        if not is_degenerate:
            return None

        # Pinning exemption: FIRST instance stays OUTSIDE.
        # Find all knowledge facts that appeared BEFORE this segment.
        seen_knowledge: set[tuple[str, str]] = set()
        first_event_seq = segment_events[0].seq
        for e in events:
            if e.seq == first_event_seq:
                break
            if isinstance(e, KnowledgeEvent):
                h = hashlib.sha256(e.snippet.strip().encode()).hexdigest()
                seen_knowledge.add((e.scope, h))

        # Move the span start forward past any "first instances" of knowledge in the segment.
        # We only need to clear the start to respect "stays OUTSIDE" for the first half.
        # Interleaved first-instances in the middle are protected by View.of pinning.
        span_start_idx = 0
        while span_start_idx < len(segment_events):
            e = segment_events[span_start_idx]
            if isinstance(e, KnowledgeEvent):
                h = hashlib.sha256(e.snippet.strip().encode()).hexdigest()
                key = (e.scope, h)
                if key not in seen_knowledge:
                    seen_knowledge.add(key)
                    span_start_idx += 1
                    continue
            break

        final_span = segment_events[span_start_idx:]
        if not final_span:
            return None

        start_seq = final_span[0].seq
        end_seq = final_span[-1].seq

        if start_seq is None or end_seq is None:
            return None

        # Summary counts (for the tombstone text)
        m_dupes = sum(1 for e in final_span if isinstance(e, KnowledgeEvent))
        k_plans = sum(1 for e in final_span if isinstance(e, PlanEvent))

        summary = (
            f"[Condensed {len(final_span)} degenerate turns: the agent repeated itself "
            f"without calling any tools ({m_dupes} duplicate knowledge entries, "
            f"{k_plans} plan revisions). No work was performed in this span. "
            "Do not imitate this pattern — proceed by calling tools.]"
        )

        return CondensationEvent(
            forgotten_start_seq=start_seq,
            forgotten_end_seq=end_seq,
            summary=summary,
            summary_role="user",
            reason="hard_reset",
        )

    async def _reconstruct_resume_context(self, conversation_id: str, events: list) -> list:
        """Build the events to append before a resume status flip (DC-05b / DEFECT-4).

        Returns a list that may contain:
          - 0 or more synthetic ObservationEvents for dangling (unresolved) actions
          - exactly 1 ENVIRONMENT MessageEvent: sandbox reality + file list + sessions
            + the first undone plan step as the next actionable instruction

        Receives the event list explicitly so it is unit-testable against an archived
        log without a live runtime — do NOT load events from the store here.
        """
        result: list = []

        # 1. Synthesize terminal observations for dangling actions.
        # An action is "dangling" if no ObservationEvent or AgentErrorEvent references
        # its id — the server was killed while the tool was in-flight.
        resolved: set[str] = set()
        for e in events:
            if isinstance(e, ObservationEvent) and e.action_id:
                resolved.add(e.action_id)
            elif isinstance(e, AgentErrorEvent) and e.action_id:
                resolved.add(e.action_id)

        for e in events:
            if isinstance(e, ActionEvent) and e.id not in resolved:
                result.append(
                    ObservationEvent(
                        source=EventSource.ENVIRONMENT,
                        action_id=e.id,
                        # Preserve the action's generation so strict histories
                        # retain the synthetic result beside its tool call.  An
                        # untagged result would be correctly quarantined as late
                        # legacy output and leave an invalid orphaned tool call in
                        # the model view after resume.
                        agent_view_id=e.agent_view_id,
                        tool_result=ToolResult(
                            call_id=e.tool_call.call_id,
                            tool_name=e.tool_call.tool_name,
                            success=False,
                            content=(
                                "<system-reminder>This action was interrupted by a server"
                                " restart — its outcome is UNKNOWN. Re-verify its effect"
                                " before assuming it completed.</system-reminder>"
                            ),
                        ),
                    )
                )

        # 2. Environment reality block.
        # Starts with "Resumed by user." so existing checks that test for that
        # literal substring continue to pass.
        parts: list[str] = ["Resumed by user. Current environment reality after interruption:"]

        # The sandbox sentence must match reality: a PAUSED landed by an in-loop
        # valve (dc-05a actionless/noop breakers) leaves the executor — and its
        # sandbox — alive; only restart/suspend paths reclaim it. Lying about a
        # reclaim would push the model into pointless re-verification.
        if conversation_id in self._rt._executors:
            parts.append(
                "- Your sandbox is still running — existing workspace files and"
                " shell sessions are intact."
            )
        else:
            parts.append(
                "- The previous sandbox was reclaimed. A fresh sandbox is created on"
                " your next action and your saved workspace files are restored into"
                " it automatically."
            )

        # Workspace file listing from the project-store snapshot (up to 30 paths).
        store = self._rt._project_store_now()
        file_paths: list[str] = []
        if store is not None and store.status() == StorageStatus.OK:
            try:
                workspace = store.path_for(conversation_id)
                raw_paths = list(store.iter_workspace(conversation_id))[:30]
                file_paths = [str(p.relative_to(workspace)) for p in raw_paths]
            except Exception:  # noqa: BLE001 — missing/corrupt store: fall through to empty
                pass

        if file_paths:
            listing = "\n  ".join(file_paths)
            parts.append(f"- Files that will be restored:\n  {listing}")
        else:
            parts.append("- No saved files — the workspace starts empty.")

        # DC-07: List uploads held server-side.
        upload_names = sorted(list(self._rt.get_upload_names(conversation_id)))
        if upload_names:
            # If the sandbox is dead, they are "lost-and-recoverable" until the
            # next action triggers recreation + re-materialization.
            listing = "\n  ".join(f"uploads/{n}" for n in upload_names[:30])
            status = (
                "intact"
                if conversation_id in self._rt._executors
                else "held server-side and will be restored"
            )
            parts.append(f"- Uploaded files ({status}):\n  {listing}")

        # Session list (degrades gracefully — sessions_snapshot never raises).
        sessions, _ = await self._rt.sessions_snapshot(conversation_id)
        if sessions:
            names = ", ".join(s.name for s in sessions)
            parts.append(f"- Shell sessions: {names}")
        else:
            parts.append("- No shell sessions are running.")

        # 3. Plan restatement: the first step not yet done, from the MERGED progress
        # source (plan_step + update_plan_progress) so a capable model's declarative
        # marks aren't ignored on resume — else it's told to redo an already-done step.
        latest_plan, _states = effective_plan_progress(events)
        if latest_plan is not None and latest_plan.steps:
            first_undone: int | None = None
            for i in range(1, len(latest_plan.steps) + 1):
                if _states.get(i) != "done":
                    first_undone = i
                    break

            if first_undone is not None:
                step_title = latest_plan.steps[first_undone - 1].title
                parts.append(
                    f"Next actionable step ({first_undone}): '{step_title}'."
                    " Do not re-plan, do not summarize, and do not ask the user"
                    " anything — everything you need is in this message. Execute"
                    " this step now using tools."
                )

        result.append(
            MessageEvent(
                source=EventSource.ENVIRONMENT,
                message=LLMMessage(role="user", content="\n".join(parts)),
            )
        )
        return result

    async def resume_conversation(self, conversation_id: str) -> dict:
        """Mode-agnostic, event-log-driven resume. Legal from PAUSED, a terminal
        ERROR or STUCK (the recovery path), and IDLE-with-unfinished-plan (an approved
        PlanEvent exists but FINISHED was never reached).

        Returns {"ok": True, "status": "RUNNING"} on success or {"ok": False, "reason": …}
        for illegal transitions — the HTTP route converts non-ok to 409.

        ERROR / STUCK are RECOVERABLE (the "Try again" button): a build/agent run that
        crashed or wedged keeps its full persisted history + workspace snapshot. Resuming
        flips it back to RUNNING and re-kicks the loop, which rehydrates the workspace and
        continues from the last event — progress is NEVER discarded. If the underlying
        cause persists (e.g. a still-dead driver/sandbox), the loop's own pre-flight
        re-terminalizes honestly, but the history is intact and the user can retry again.

        Deep Research conversations dispatch to the EXISTING DR resume behavior unchanged
        (mode check). Build/Research: flip PAUSED/ERROR/STUCK/IDLE → RUNNING in the event
        log so loop.run() doesn't early-return on a parked/terminal status, then kick via
        the standard task-spawn path (same as send-message). kick() is idempotent — no
        second loop if one is live (a genuinely RUNNING run is rejected up front).
        """
        # The unfinished-plan predicate stays a module-level helper in runtime.py;
        # reach it late-bound to avoid a module-load circular import.
        from .runtime import _has_unfinished_plan

        surface = self._rt._surface_of(conversation_id)

        def rejection(status: ConversationStatus, events: list) -> str | None:
            """Return the stable public reason for a non-resumable fresh head."""

            if status == ConversationStatus.RUNNING:
                return "already_running"
            if status == ConversationStatus.FINISHED:
                return "conversation_finished"
            if status == ConversationStatus.PAUSED:
                return None
            if status in (ConversationStatus.ERROR, ConversationStatus.STUCK):
                return None if surface != "deep_research" else f"illegal_state_{status.value}"
            if status == ConversationStatus.IDLE and _has_unfinished_plan(events):
                return None
            return f"illegal_state_{status.value}"

        # Phase 1: bind the resume to the exact task/generation visible at a
        # fenced, resumable head.  Detaching happens synchronously under the
        # fence, so a later ingress can register a new task without this resume
        # ever popping or cancelling it.  The detached task is only the old
        # cooperative-stop tail we observed here.
        async with self._rt.workspace_lock(conversation_id):
            async with self._rt._workspace.interprocess_mutation_fence(conversation_id):
                observed_task = self._rt._tasks.get(conversation_id)
                observed_generation = self._rt._run_generation.get(conversation_id)
                state = await self._rt._store.get_state(conversation_id)
                events = agent_view_consistent_events(
                    await self._rt._store.get_events(conversation_id)
                )
                reason = rejection(state.execution_status, events)
                if reason is not None:
                    return {"ok": False, "reason": reason}
                detached, finishing_task = self._detach_finishing_task_locked(
                    conversation_id,
                    observed_task,
                    observed_generation,
                )
                if not detached:
                    return {"ok": False, "reason": "resume_superseded"}

        # WALK-18: wait outside the workspace fence because the old task may need
        # that fence for cancellation cleanup.  The helper receives the exact
        # detached task—not a conversation id—and therefore cannot touch a newer
        # task that may be registered while this await yields.
        drained = await self._drain_finishing_task(finishing_task)
        if not drained:
            # Keep a cancellation-resistant old task visible to kick/suspend
            # guards. Never start a second run while it remains alive.
            async with self._rt.workspace_lock(conversation_id):
                async with self._rt._workspace.interprocess_mutation_fence(conversation_id):
                    superseded = (
                        self._rt._run_generation.get(conversation_id) != observed_generation
                        or self._rt._tasks.get(conversation_id) is not None
                    )
                    if not superseded and finishing_task is not None:
                        self._rt._tasks[conversation_id] = finishing_task
            return {
                "ok": False,
                "reason": "resume_superseded" if superseded else "resume_drain_timeout",
            }

        # Phase 2: re-enter both fences and derive everything from the fresh
        # durable head.  A task/generation that appeared while the old task was
        # draining owns the conversation; this stale resume must not clear its
        # flags or append reconstruction over it.
        async with self._rt.workspace_lock(conversation_id):
            async with self._rt._workspace.interprocess_mutation_fence(conversation_id):
                current_task = self._rt._tasks.get(conversation_id)
                if self._rt._run_generation.get(conversation_id) != observed_generation:
                    return {"ok": False, "reason": "resume_superseded"}
                if current_task is not None and not current_task.done():
                    return {"ok": False, "reason": "already_running"}

                state = await self._rt._store.get_state(conversation_id)
                events = agent_view_consistent_events(
                    await self._rt._store.get_events(conversation_id)
                )
                reason = rejection(state.execution_status, events)
                if reason is not None:
                    return {"ok": False, "reason": reason}

                # DC-05c and reconstruction use this fenced, post-drain history.
                # Keep the tombstone in the same atomic append as the synthetic
                # results, run intent, and RUNNING flip—no half-resumed log.
                tombstone = self._condense_trailing_degeneracy(events)
                reconstruction_history = [*events]
                if tombstone is not None:
                    reconstruction_history.append(tombstone)
                new_events = await self._reconstruct_resume_context(
                    conversation_id,
                    reconstruction_history,
                )

                # A direct/nonstandard task starter does not necessarily honor
                # the workspace lock. Recheck the exact runtime identity after
                # reconstruction's awaited environment probes and immediately
                # before mutating volatile flags or the append-only log.
                current_task = self._rt._tasks.get(conversation_id)
                if self._rt._run_generation.get(conversation_id) != observed_generation:
                    return {"ok": False, "reason": "resume_superseded"}
                if current_task is not None and not current_task.done():
                    return {"ok": False, "reason": "already_running"}

                # Clear only the old run's cooperative-stop state after the final
                # authority check. A losing resume never mutates a newer loop.
                self._rt._cancel_flags.pop(conversation_id, None)
                loop = self._rt._loops.get(conversation_id)
                if loop is not None and hasattr(loop, "_pause_requested"):
                    loop._pause_requested.clear()

                pending = [*([tombstone] if tombstone is not None else []), *new_events]
                if surface != "deep_research":
                    if surface in self._rt._BUILD_LIKE_SURFACES:
                        from disco.core import WorkspaceMutationEvent

                        pending.append(
                            WorkspaceMutationEvent(
                                operation="agent.run-intent.resume",
                                run_protocol_version=1,
                            )
                        )
                    pending.append(StatusEvent(status=ConversationStatus.RUNNING, detail="resumed"))
                await self._rt._store.append_many(conversation_id, pending)

        # Route the resume through the PINNED start path (finding #2), NOT a raw
        # `kick`. A bare kick starts a NEW run UNPINNED. `runtime.start` resolves +
        # pins the selected kernel (reusing an existing pin from a PAUSED gate-park)
        # and routes through it; for the default `disco` kernel `start` is a
        # behaviour-identical pass-through to `kick`.
        self._rt.start(conversation_id)
        return {"ok": True, "status": "RUNNING"}
