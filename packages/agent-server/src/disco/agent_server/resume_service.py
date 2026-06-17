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
)
from disco.core.loop.engine import _BOOKKEEPING_TOOLS
from disco.tools.projects import StorageStatus


class ResumeService:
    """Resume reconstruction logic; live runtime state via the back-ref."""

    def __init__(self, rt: Any) -> None:
        self._rt = rt

    def _condense_trailing_degeneracy(
        self, events: list
    ) -> CondensationEvent | None:
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

    async def _reconstruct_resume_context(
        self, conversation_id: str, events: list
    ) -> list:
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
        parts: list[str] = [
            "Resumed by user. Current environment reality after interruption:"
        ]

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

        # 3. Plan restatement: find the latest plan and the first step not yet done.
        latest_plan: PlanEvent | None = None
        for e in events:
            if isinstance(e, PlanEvent):
                if latest_plan is None or e.revision >= latest_plan.revision:
                    latest_plan = e

        if latest_plan is not None and latest_plan.steps:
            plan_seq = latest_plan.seq or 0
            done_steps: set[int] = set()
            for e in events:
                if not isinstance(e, ActionEvent) or e.tool_call is None:
                    continue
                if e.tool_call.tool_name != "plan_step":
                    continue
                if (e.seq or 0) < plan_seq:
                    continue
                try:
                    idx = int(e.tool_call.arguments.get("index"))  # type: ignore[arg-type]
                    state = str(e.tool_call.arguments.get("state"))
                except (TypeError, ValueError):
                    continue
                if state == "done":
                    done_steps.add(idx)

            first_undone: int | None = None
            for i in range(1, len(latest_plan.steps) + 1):
                if i not in done_steps:
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
        """Mode-agnostic, event-log-driven resume. Legal from PAUSED and IDLE-with-
        unfinished-plan (an approved PlanEvent exists but FINISHED was never reached).

        Returns {"ok": True, "status": "RUNNING"} on success or {"ok": False, "reason": …}
        for illegal transitions — the HTTP route converts non-ok to 409.

        Deep Research conversations dispatch to the EXISTING DR resume behavior unchanged
        (mode check). Build/Research: flip PAUSED/IDLE → RUNNING in the event log so
        loop.run() doesn't early-return on PAUSED, then kick via the standard task-spawn
        path (same as send-message). kick() is idempotent — no second loop if one is live.
        """
        # The unfinished-plan predicate stays a module-level helper in runtime.py;
        # reach it late-bound to avoid a module-load circular import.
        from .runtime import _has_unfinished_plan

        state = await self._rt._store.get_state(conversation_id)
        status = state.execution_status

        if status == ConversationStatus.RUNNING:
            return {"ok": False, "reason": "already_running"}
        if status == ConversationStatus.FINISHED:
            return {"ok": False, "reason": "conversation_finished"}
        if status == ConversationStatus.ERROR:
            return {"ok": False, "reason": "conversation_error"}

        events = await self._rt._store.get_events(conversation_id)

        # DC-05c: condense trailing degeneracy before reconstruction
        tombstone = self._condense_trailing_degeneracy(events)
        if tombstone is not None:
            await self._rt._store.append(conversation_id, tombstone)
            events = await self._rt._store.get_events(conversation_id)

        legal = status == ConversationStatus.PAUSED
        if not legal and status == ConversationStatus.IDLE:
            legal = _has_unfinished_plan(events)

        if not legal:
            return {"ok": False, "reason": f"illegal_state_{status.value}"}

        # WALK-18 — drain any lingering loop task from a cooperative Stop BEFORE
        # we reconstruct context or flip to RUNNING. Otherwise kick() no-ops over
        # the still-finishing task (resume silently does nothing), and a flip to
        # RUNNING before the old task checkpoints would make it keep running.
        await self._rt._drain_finishing_task(conversation_id)

        # Reconstruct resume context: synthesized observations + reality block.
        # All new events are appended BEFORE the RUNNING status flip so View.of
        # sees resolved action→observation pairs and the model re-orients correctly.
        new_events = await self._reconstruct_resume_context(conversation_id, events)
        for event in new_events:
            await self._rt._store.append(conversation_id, event)

        # Clear any lingering cancel flag from a prior Stop.
        self._rt._cancel_flags.pop(conversation_id, None)
        # WALK-18 — cancel a pending cooperative pause on a cached loop so the
        # re-kick isn't immediately re-paused at its first checkpoint (the
        # already-consumed-pause case cleared it at the checkpoint; this covers a
        # pause that was requested but never reached a checkpoint to land).
        _loop = self._rt._loops.get(conversation_id)
        if _loop is not None and hasattr(_loop, "_pause_requested"):
            _loop._pause_requested.clear()

        surface = self._rt._surface_of(conversation_id)
        if surface == "deep_research":
            # DR: _maybe_run_deep_research detects PAUSED and re-runs from the partial
            # ReportEvent checkpoint — dispatch unchanged, do not touch DR internals.
            pass
        else:
            # Build/Research: flip to RUNNING so loop.run() doesn't early-return on PAUSED.
            # The loop emits another RUNNING at the top of run() — idempotent.
            await self._rt._store.append(
                conversation_id,
                StatusEvent(status=ConversationStatus.RUNNING, detail="resumed"),
            )

        self._rt.kick(conversation_id)
        return {"ok": True, "status": "RUNNING"}
