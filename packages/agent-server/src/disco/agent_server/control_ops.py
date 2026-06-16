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
    StatusEvent,
)


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

        Composes the loop lazily (`_loop_for`, same as `kick`) — a fresh
        conversation or one whose loop died with a server restart has no entry in
        `_loops`, and the old `.get()` guard silently dropped the frame: the
        composer showed "sending…" forever while the server did nothing."""
        loop = self._rt._loop_for(conversation_id)
        await loop.enter_planning(text)
        self._rt.kick(conversation_id)  # produce the (revised) plan

    async def cancel(self, conversation_id: str) -> None:
        """Cooperative stop (distinct from the hard kill): the loop winds down. For
        Deep Research (engine, not an AgentLoop) this ALSO sets the cancel flag the
        engine polls — without it, Stop was a no-op (the engine ran to completion)."""
        self._rt._cancel_flags.setdefault(conversation_id, asyncio.Event()).set()
        loop = self._rt._loops.get(conversation_id)
        if loop is not None:
            await loop.cancel()

    async def kill(self, conversation_id: str) -> None:
        """The KILL SWITCH (BoD §13.6) — the ultimate stop above the three security
        layers. Halts a RUNNING loop promptly (cancel the task mid-step), revokes the
        agent's capabilities + tears down the sandbox session (executor.kill), and
        records a terminal status so the UI reflects the stop."""
        # 1. stop the running loop task promptly — do NOT wait for the current step.
        task = self._rt._tasks.pop(conversation_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # 2. revoke capabilities + destroy the sandbox (the executor's kill, §6.4),
        #    then DROP the executor + loop from the caches. Critical: a killed executor
        #    is permanently `_killed=True` and returns "executor killed; instance
        #    revoked" for every call — if it stayed cached, RESUMING the conversation
        #    would reuse the dead executor and every tool call would fail forever (the
        #    exact unrecoverable loop a build hit). Popping them forces `_loop_for` to
        #    rebuild a FRESH executor + sandbox on the next run.
        executor = self._rt._executors.pop(conversation_id, None)
        if executor is not None:
            await executor.kill()
        pending = self._rt._pending_sessions.pop(conversation_id, None)
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.destroy()
        self._rt._loops.pop(conversation_id, None)
        # 3. record the stop so subscribers see it (no STOPPED status in the enum; IDLE
        #    + a 'killed' detail is the contract's terminal-for-now shape).
        await self._rt._store.append(
            conversation_id,
            StatusEvent(
                source=EventSource.SYSTEM, status=ConversationStatus.IDLE, detail="killed"
            ),
        )
