"""Plan/action gates and cooperative conversation controls."""

from __future__ import annotations

from typing import Protocol

from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    ToolCall,
)
from disco.core.store.sqlite import SqliteEventStore
from disco.retrieval.deep_research import (
    STOP_REQUESTED_ACTION,
    stop_requested_payload,
)

from .lifecycle_command_service import LifecycleCommandService
from .run_controller import RunController
from .run_kill_service import RunKillService
from .run_registry import CancellationRegistry, LoopRegistry
from .workspace_service import WorkspaceCoordinator


class LiveDeepResearchRuns(Protocol):
    """Whether the deep-research engine — not an `AgentLoop` — owns this run."""

    def has_live_run(self, conversation_id: str) -> bool: ...

# ---- F-3: conservative "stop, accept as-is" intent list ----------------------
# Matched case-insensitively against the full (stripped) user text.
# ONLY whole-intent stop phrases are listed — bare "publish it", "deploy it",
# or any real change/deploy intent intentionally fall through to the replan path.
# The clean signal now exists: the explicit `accept_finished` client frame
# (wire.py → `ControlOps.accept_finished`), sent by the UI's Mark-done control,
# states stop-intent without any guessing. This free-text heuristic is KEPT as
# the fallback for plain typed messages ("ship it" in the re-plan composer).
# SUBSTRING-safe: specific, terminal multi-word phrases that do not precede a new
# instruction, so they are matched ANYWHERE in the message — this catches the real
# traced phrasing "stop troubleshooting, publish it and be done" (a leading clause +
# the terminal stop phrase), which an exact-only match would miss.
_SHIP_IT_CONTAINS: tuple[str, ...] = (
    "publish it and be done",
    "leave it as is",
    "leave it as-is",
    "leave it as it is",
    "mark it done",
    "mark it as done",
    "mark it complete",
    "mark it as complete",
    "mark as complete",
    "call it done",
    "be done with it",
)
# WHOLE-MESSAGE only: shorter phrases that could false-match mid-sentence inside a real
# change request ("you're done with the header, now add a footer"), so they only count
# when they ARE the entire message.
_SHIP_IT_EXACT: frozenset[str] = frozenset(
    {
        "you're done",
        "you are done",
        "that's done",
        "we're done",
        "we are done",
        "it's done",
        "done",
        "ship it",
        "call it",
    }
)


def _is_ship_it_intent(text: str) -> bool:
    """Conservative 'stop, accept as-is' check. True ONLY for clear stop intents;
    default False (fall through to re-plan). Bare 'publish it', 'deploy it',
    'publish to Netlify', and any change request intentionally fall through.

    Terminal multi-word phrases match anywhere (so a leading clause like
    'stop troubleshooting, …' still counts); short/ambiguous phrases must be the
    whole message. Whitespace collapsed + trailing punctuation stripped."""
    norm = " ".join(text.strip().lower().split()).rstrip(".!?")
    if norm in _SHIP_IT_EXACT:
        return True
    return any(p in norm for p in _SHIP_IT_CONTAINS)


class ControlOps:
    def __init__(
        self,
        store: SqliteEventStore,
        workspace: WorkspaceCoordinator,
        loops: LoopRegistry,
        cancellations: CancellationRegistry,
        controller: RunController,
        kills: RunKillService,
        deep_research: LiveDeepResearchRuns,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._loops = loops
        self._cancellations = cancellations
        self._controller = controller
        self._kills = kills
        self._deep_research = deep_research

    async def confirm(self, conversation_id: str) -> None:
        """Approve the pending action: execute EXACTLY it (the loop's `confirm`), then
        resume the loop for the next steps.

        Lazy-composes (`_loop_for`) like `request_plan` below: after a server restart
        `_loops` is empty, and the old `.get()` guard SILENTLY dropped the frame — the
        user clicked Approve and nothing happened (state is event-sourced, so the
        recomposed loop sees the same pending gate)."""
        loop = self._controller.loop_for(conversation_id)
        await loop.confirm()
        self._controller.kick(conversation_id)  # continue plan→act→observe past the gate

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        """Deny the pending action: record the denial (no execution), then resume.
        Lazy-composes — see `confirm` (the post-restart silent-drop hole)."""
        loop = self._controller.loop_for(conversation_id)
        await loop.reject(reason)
        self._controller.kick(conversation_id)

    async def approve_plan(self, conversation_id: str) -> None:
        """Approve the pending plan: flip the loop into execution mode (full tools)
        and run it. The per-action BlastRadiusConfirm gate still governs the build.
        Lazy-composes — see `confirm` (the post-restart silent-drop hole)."""
        loop = self._controller.loop_for(conversation_id)
        await loop.approve_plan()
        self._controller.kick(conversation_id)  # start building the approved plan

    async def accept_finished(self, conversation_id: str, text: str = "") -> None:
        """Explicit UI stop-intent (the `accept_finished` frame): the user accepted
        the FINISHED build as-is. AUTHORITATIVE — the text is never inspected for
        intent, so it always wins over conflicting prose (a note like "add a
        footer" riding this frame is an acknowledgment, not a replan request; a
        typed replan goes through `request_plan` instead).

        Mirrors the F-3 ship-it branch of `request_plan` under the same workspace
        lock: append the optional user note (resolving any optimistic UI echo)
        and leave the conversation FINISHED — no planning ingress, no run-intent,
        no kick. A no-op when the conversation is NOT FINISHED: there is nothing
        to accept, and this control must never wind down or redirect a live run
        (`pause`/`cancel` own that)."""
        async with self._workspace.lock(conversation_id):
            state = await self._store.get_state(conversation_id)
            if state.execution_status != ConversationStatus.FINISHED:
                return
            if text.strip():
                await self._store.append(
                    conversation_id,
                    MessageEvent(
                        source=EventSource.USER,
                        message=LLMMessage(role="user", content=text),
                    ),
                )

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        """(Re-)enter plan mode with the user's instruction — the first plan AND the
        re-plan after a build, so focused diff-style changes are planned and re-approved
        instead of free-form steered.

        F-3 exception: when the conversation is already FINISHED and the user's text
        is a clear 'stop, accept as-is' intent (_SHIP_IT_PHRASES), append the user
        message (resolving the optimistic UI echo) but skip enter_planning() and
        kick() — the build stays FINISHED. Any ambiguous text falls through to the
        existing replan path unchanged. (The UI's Mark-done control states the
        same intent explicitly via `accept_finished` above — this heuristic is
        the fallback for plain typed messages only.)

        Composes the loop lazily (`_loop_for`, same as `kick`) — a fresh
        conversation or one whose loop died with a server restart has no entry in
        `_loops`, and the old `.get()` guard silently dropped the frame: the
        composer showed "sending…" forever while the server did nothing."""
        # Decide stop-vs-run and durably invalidate an older workspace seal under
        # the same permanent fence. The explicit ship-it branch is acknowledgment,
        # not execution intent, so it deliberately remains seal-compatible.
        async with self._workspace.lock(conversation_id):
            state = await self._store.get_state(conversation_id)
            if state.execution_status == ConversationStatus.FINISHED and _is_ship_it_intent(text):
                if text.strip():
                    await self._store.append(
                        conversation_id,
                        MessageEvent(
                            source=EventSource.USER,
                            message=LLMMessage(role="user", content=text),
                        ),
                    )
                return
            async with self._workspace.interprocess_mutation_fence(conversation_id):
                pending = []
                if text.strip():
                    pending.append(
                        MessageEvent(
                            source=EventSource.USER,
                            message=LLMMessage(role="user", content=text),
                        )
                    )
                # Publish the planning-mode marker in the same transaction as
                # the instruction and v1 run intent. A worker can therefore see
                # either the old execution state or the complete planning
                # ingress, never the new instruction under the old tool scope.
                pending.append(
                    LifecycleCommandService.build_status(
                        ConversationStatus.RUNNING,
                        detail="planning",
                    )
                )
                await self._workspace.append_run_ingress_locked(
                    conversation_id,
                    pending,
                    "request-plan",
                )
        events = await self._store.get_events(conversation_id)
        claimed_user_seq = max(
            (
                event.seq or 0
                for event in events
                if isinstance(event, MessageEvent) and event.source is EventSource.USER
            ),
            default=None,
        )
        if claimed_user_seq is None:
            self._controller.kick(conversation_id)
        else:
            self._controller.kick(
                conversation_id,
                claimed_user_seq=claimed_user_seq,
            )

    async def pause(self, conversation_id: str) -> None:
        """WALK-18 cooperative pause: set the loop's pause flag so it lands PAUSED
        at the next step boundary (resume re-kicks). Unlike `cancel`, this takes
        NO lock and emits no terminal status — it must not contend with the
        in-flight model step. A no-op when no live loop exists (nothing running
        to pause); the only loop that matters is an in-memory, running one."""
        loop = self._loops.loop(conversation_id)
        if loop is not None:
            await loop.pause()

    async def cancel(self, conversation_id: str) -> None:
        """Cooperative stop (distinct from the hard kill): the run winds down.

        Two runtimes own runs here, and the flag is set for both: an
        `AgentLoop` (build/agent/research) and the deep-research ENGINE. For a
        loop, `loop.cancel()` emits the terminal IDLE/"cancelled" — the loop
        really has ended by the time it returns to its own checkpoint.

        For a LIVE deep-research run that same call was a statement about
        nothing. The conversation's loop exists but never ran the research
        (`_compose_deep_research_loop`), so IDLE landed 29 ms after Stop while
        the engine kept working for another eight minutes, and the terminal
        status erased the run controls and the pending checkpoint with it. So
        this branch emits no status at all: it appends ONE non-terminal marker
        saying Stop was pressed and when, and the engine — which polls the flag
        at every boundary it has — writes the run's real ending itself
        (`research_checkpoint` → PAUSED/"stopped", resumable).
        """
        self._cancellations.request(conversation_id)
        if self._deep_research.has_live_run(conversation_id):
            await self._store.append(
                conversation_id,
                ActionEvent(
                    thought="Deep Research: stop_requested",
                    tool_call=ToolCall(
                        tool_name=STOP_REQUESTED_ACTION,
                        arguments=stop_requested_payload(),
                    ),
                ),
            )
            return
        loop = self._loops.loop(conversation_id)
        if loop is not None:
            await loop.cancel()

    async def kill(self, conversation_id: str, generation: int | None = None) -> None:
        await self._kills.kill(conversation_id, generation)
