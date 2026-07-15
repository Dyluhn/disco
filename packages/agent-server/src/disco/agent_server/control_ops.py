"""Conversation control ops — the confirmation gate + kill switch
(BoD §13.4/§13.6) — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The conversation
control ops that route to the loop move out of runtime.py into a `ControlOps`
collaborator constructed once in `ConversationRuntime`: the plan/action gate
(`confirm` / `reject` / `approve_plan` / `request_plan`), the cooperative stop
(`cancel`), and the hard kill switch (`kill`).

The service reaches the runtime's live state (`_loops`, `_tasks`, `_executors`,
`_pending_sessions`, `_cancel_flags`, `_store`) + the `_loop_for` / `kick`
resolvers via a back-reference. Every method keeps a one-line delegator on
`ConversationRuntime` because the WS routes call each on the runtime.
`pick_alternative` / `resume` / the resume-reconstruction trio stay on the
runtime (out of this collaborator's scope).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)

# ---- F-3: conservative "stop, accept as-is" intent list ----------------------
# Matched case-insensitively against the full (stripped) user text.
# ONLY whole-intent stop phrases are listed — bare "publish it", "deploy it",
# or any real change/deploy intent intentionally fall through to the replan path.
# TODO: the cleaner long-term signal is an explicit UI `accept_finished`/
# `mark_done` frame action rather than free-text matching. This is a stopgap.
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
    def __init__(self, rt: Any) -> None:
        self._rt = rt

    async def confirm(self, conversation_id: str) -> None:
        """Approve the pending action: execute EXACTLY it (the loop's `confirm`), then
        resume the loop for the next steps.

        Lazy-composes (`_loop_for`) like `request_plan` below: after a server restart
        `_loops` is empty, and the old `.get()` guard SILENTLY dropped the frame — the
        user clicked Approve and nothing happened (state is event-sourced, so the
        recomposed loop sees the same pending gate)."""
        loop = self._rt._loop_for(conversation_id)
        await loop.confirm()
        self._rt.kick(conversation_id)  # continue plan→act→observe past the gate

    async def reject(self, conversation_id: str, reason: str = "rejected by user") -> None:
        """Deny the pending action: record the denial (no execution), then resume.
        Lazy-composes — see `confirm` (the post-restart silent-drop hole)."""
        loop = self._rt._loop_for(conversation_id)
        await loop.reject(reason)
        self._rt.kick(conversation_id)

    async def approve_plan(self, conversation_id: str) -> None:
        """Approve the pending plan: flip the loop into execution mode (full tools)
        and run it. The per-action BlastRadiusConfirm gate still governs the build.
        Lazy-composes — see `confirm` (the post-restart silent-drop hole)."""
        loop = self._rt._loop_for(conversation_id)
        await loop.approve_plan()
        self._rt.kick(conversation_id)  # start building the approved plan

    async def request_plan(self, conversation_id: str, text: str = "") -> None:
        """(Re-)enter plan mode with the user's instruction — the first plan AND the
        re-plan after a build, so focused diff-style changes are planned and re-approved
        instead of free-form steered.

        F-3 exception: when the conversation is already FINISHED and the user's text
        is a clear 'stop, accept as-is' intent (_SHIP_IT_PHRASES), append the user
        message (resolving the optimistic UI echo) but skip enter_planning() and
        kick() — the build stays FINISHED. Any ambiguous text falls through to the
        existing replan path unchanged.

        Composes the loop lazily (`_loop_for`, same as `kick`) — a fresh
        conversation or one whose loop died with a server restart has no entry in
        `_loops`, and the old `.get()` guard silently dropped the frame: the
        composer showed "sending…" forever while the server did nothing."""
        # F-3: ONLY when already FINISHED + a clear stop-intent — keep FINISHED.
        state = await self._rt._store.get_state(conversation_id)
        if state.execution_status == ConversationStatus.FINISHED and _is_ship_it_intent(text):
            # Append the user message so the optimistic UI echo resolves (same
            # append that enter_planning would do), but skip enter_planning + kick.
            if text.strip():
                await self._rt._store.append(
                    conversation_id,
                    MessageEvent(
                        source=EventSource.USER,
                        message=LLMMessage(role="user", content=text),
                    ),
                )
            return
        loop = self._rt._loop_for(conversation_id)
        await loop.enter_planning(text)
        events = await self._rt._store.get_events(conversation_id)
        claimed_user_seq = max(
            (
                event.seq or 0
                for event in events
                if isinstance(event, MessageEvent) and event.source is EventSource.USER
            ),
            default=None,
        )
        if claimed_user_seq is None:
            self._rt.kick(conversation_id)
        else:
            self._rt.kick(
                conversation_id,
                claimed_user_seq=claimed_user_seq,
            )

    async def pause(self, conversation_id: str) -> None:
        """WALK-18 cooperative pause: set the loop's pause flag so it lands PAUSED
        at the next step boundary (resume re-kicks). Unlike `cancel`, this takes
        NO lock and emits no terminal status — it must not contend with the
        in-flight model step. A no-op when no live loop exists (nothing running
        to pause); the only loop that matters is an in-memory, running one."""
        loop = self._rt._loops.get(conversation_id)
        if loop is not None:
            await loop.pause()

    async def cancel(self, conversation_id: str) -> None:
        """Cooperative stop (distinct from the hard kill): the loop winds down. For
        Deep Research (engine, not an AgentLoop) this ALSO sets the cancel flag the
        engine polls — without it, Stop was a no-op (the engine ran to completion)."""
        self._rt._cancel_flags.setdefault(conversation_id, asyncio.Event()).set()
        loop = self._rt._loops.get(conversation_id)
        if loop is not None:
            await loop.cancel()

    def _superseded_by_newer_run(self, conversation_id: str, generation: int | None) -> bool:
        """True when a NEWER run/generation has taken over the conversation since this
        kill was issued against `generation` (finding #4 — the LAST terminalizer path).
        `generation is None` (a legacy / direct caller with no competing newer run) is
        NEVER superseded → terminalize as before. Mirrors the guard predicate of
        `_unpin_if_current_generation` so the kill path stays consistent with the
        clean-return / crash / abandoned-gate terminalizers."""
        return (
            generation is not None and self._rt._run_generation.get(conversation_id) != generation
        )

    async def kill(self, conversation_id: str, generation: int | None = None) -> None:
        """The KILL SWITCH (BoD §13.6) — the ultimate stop above the three security
        layers. Halts a RUNNING loop promptly (cancel the task mid-step), revokes the
        agent's capabilities + tears down the sandbox session (executor.kill), and
        records a terminal status so the UI reflects the stop.

        Generation-guarded (finding #4 — made symmetric with the other three
        terminalizers). The teardown below AWAITS the killed task (and the executor /
        sandbox teardown), and in those await windows a fresh user turn can start a NEWER
        run (generation N+1) that REUSES this conversation's cached loop/executor/session
        and pin. A stale kill must then neither tear down the newer run's executor nor
        append the terminal IDLE into its log. `generation` is the run-generation the
        kill was issued against (threaded from `ConversationRuntime.kill`); `None` keeps
        the legacy unconditional behavior for direct/legacy callers."""
        # 1. stop the running loop task promptly — do NOT wait for the current step.
        task = self._rt._tasks.pop(conversation_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Generation guard A: the `await task` above YIELDS the event loop, so a fresh
        # user turn may already have started a NEWER run that now OWNS this conversation
        # (reusing the still-cached loop/executor). The killed run's task is cancelled —
        # yield the conversation to the newer run: do NOT tear down its executor and do
        # NOT terminalize its log.
        if self._superseded_by_newer_run(conversation_id, generation):
            return
        await self._rt._close_dangling_actions_for_kill(conversation_id, generation)
        if self._superseded_by_newer_run(conversation_id, generation):
            return
        # 2. revoke capabilities + destroy the sandbox (the executor's kill, §6.4),
        #    then DROP the executor + loop + session from the caches. Critical: a killed
        #    executor is permanently `_killed=True` and returns "executor killed; instance
        #    revoked" for every call — if it stayed cached, RESUMING the conversation
        #    would reuse the dead executor and every tool call would fail forever (the
        #    exact unrecoverable loop a build hit). POP ALL THREE caches SYNCHRONOUSLY
        #    (no await between the pops) BEFORE awaiting their teardown, so a newer run
        #    that starts during the teardown awaits below rebuilds a FRESH executor +
        #    loop via `_loop_for` instead of grabbing these half-torn-down ones.
        executor = self._rt._executors.pop(conversation_id, None)
        pending = self._rt._pending_sessions.pop(conversation_id, None)
        self._rt._loops.pop(conversation_id, None)
        if executor is not None:
            await executor.kill()
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.destroy()
        # 3. record the stop so subscribers see it (no STOPPED status in the enum; IDLE
        #    + a 'killed' detail is the contract's terminal-for-now shape). Generation
        #    guard B: a newer run may have started during the teardown awaits above —
        #    re-check IMMEDIATELY before the append with NO await in between (like the
        #    crash/reaper paths) so a stale terminal IDLE never lands in the newer run's
        #    log.
        if self._superseded_by_newer_run(conversation_id, generation):
            return
        await self._rt._store.append(
            conversation_id,
            StatusEvent(source=EventSource.SYSTEM, status=ConversationStatus.IDLE, detail="killed"),
        )
