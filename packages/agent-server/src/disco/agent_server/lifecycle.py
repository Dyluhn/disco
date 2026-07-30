"""Sandbox lifecycle — auto-suspend, idle sweep, orphan reconcile, snapshot/
rehydrate — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The build-sandbox
lifecycle machinery moves out of runtime.py into a `LifecycleManager`
collaborator constructed once in `ConversationRuntime`:

  - auto-suspend (lifecycle G): on_connect / on_disconnect / _suspend_after_grace
    / _suspend / sandbox_state
  - idle TTL sweep: sweep_idle_once / _idle_sweep_loop
  - sandbox teardown + startup orphan reconcile: _teardown_sandbox /
    reconcile_orphaned_runs / _sweep_orphan_containers
  - workspace durability: _maybe_rehydrate / _rehydrate_after_recreate /
    _rematerialize_uploads / _maybe_snapshot

`LifecycleManager` and its cohesive collaborators reach live runtime state by
back-reference. Existing runtime delegates and test seams remain undisturbed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from disco.core import (
    DEFAULT_OWNER_ID,
    ConversationStatus,
    EventFilter,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
    WorkspaceVersionEvent,
)
from disco.core.env import disco_env
from disco.core.loop import SealabilityProbeResult
from disco.tools import ProcessSandboxService
from disco.tools.projects import (
    StorageStatus,
    rehydrate_workspace,
    snapshot_workspace,
)

from .lifecycle_command_service import LifecycleCommandService
from .workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace
from .workspace_persistence import WorkspacePersistence, probe_finish_sealability

_LOG = logging.getLogger(__name__)
_DEFAULT_IDLE_SWEEP_INTERVAL_S = 60.0
_MIN_IDLE_SWEEP_INTERVAL_S = 5.0


# P-C: the gate states a conversation parks at while waiting on the human. The
# idle sweep frees their sandbox but never resolves the gate, so they accumulate
# as open gates — sweep_abandoned_gates_once reaps the genuinely-abandoned ones.
_GATE_STATES = frozenset(
    {
        ConversationStatus.WAITING_FOR_CONFIRMATION,
        ConversationStatus.AWAITING_PLAN_APPROVAL,
        ConversationStatus.AWAITING_USER_DECISION,
        ConversationStatus.AWAITING_USER_QUESTION,
    }
)


class GateReaper:
    """P-C — reap conversations abandoned at an AWAITING_* gate past a long TTL."""

    def __init__(self, rt: Any) -> None:
        self._rt = rt

    async def sweep_abandoned_gates_once(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        """Reap conversations parked at an AWAITING_* gate that the user never answered."""
        ttl_env = disco_env("ABANDONED_GATE_TTL_S", "86400")
        assert ttl_env is not None  # default above is non-None
        ttl_s = float(ttl_env)
        now = datetime.now(tz=UTC)
        # Live-run ground truth (in-memory not-done run tasks in THIS process). A
        # gate with an active run — e.g. just resumed, a decision being processed,
        # a background step executing — must NEVER be reaped even past the TTL: its
        # last persisted event can be stale while the run is mid-flight.
        live_run_ids = self._rt.running_conversation_ids()
        reaped = 0
        cursor: str | None = None
        page = 200
        while True:
            ids = await self._rt._store.list_conversations(
                owner_id=owner_id, limit=page, cursor=cursor
            )
            if not ids:
                break
            for cid in ids:
                reaped += await self._reap_one_gate(cid, live_run_ids, ttl_s, now)
            if len(ids) < page:
                break
            cursor = str((int(cursor) if cursor else 0) + len(ids))
        if reaped:
            _LOG.info("reaped %d abandoned gate conversation(s)", reaped)
        return reaped

    async def _reap_one_gate(
        self,
        cid: str,
        live_run_ids: set[str],
        ttl_s: float,
        now: datetime,
    ) -> int:
        """Evaluate + reap a single conversation's abandoned gate. Returns 0/1.

        One unreadable conversation must never abort the sweep, but it must
        remain observable rather than failing silently. The generation re-read
        and the STUCK append are separated by NO ``await`` (mirrors
        ``_terminalize_crashed``): a newer run can never slip in between them.
        """
        try:
            state = await self._rt._store.get_state(cid)
            if state.execution_status not in _GATE_STATES:
                return 0
            if cid in live_run_ids:
                return 0  # a live in-memory run/task is driving it — not abandoned
            if self._rt._connections.has_connections(cid):
                return 0  # UI attached — not abandoned
            # Capture the conversation's run-generation at the sweep-DECISION
            # point (finding #3, SAME stale-terminalizer race as
            # `_terminalize_crashed`, third path). This sweep reads gate state /
            # liveness, then AWAITS event reads below before the terminal append.
            # In that window a newer run can start (a fresh user message resumes
            # the gate → `kick` bumps `_run_generation[cid]` and REUSES the pin).
            # Re-read the generation just before the terminal append and SKIP if
            # it changed, so the reaper never appends a STALE STUCK into the
            # NEWER run's log nor clears the newer run's pin. A genuinely-
            # abandoned gate with no in-memory run has `None` here (and still
            # `None` at re-read) → terminalizes + unpins as before.
            captured_generation = self._rt._run_generation.get(cid)
            events = await self._rt._store.get_events(
                cid,
                EventFilter(after_seq=state.last_seq - 1) if state.last_seq > 0 else None,
            )
            if not events:
                return 0
            last_ts = events[-1].timestamp
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=UTC)
            if (now - last_ts).total_seconds() < ttl_s:
                return 0
            # RE-READ state AFTER the awaits above; a newer run may have started
            # (and re-pinned) since the gate/liveness/generation snapshot. The
            # guard below + the STUCK append are separated by NO `await`, so a
            # newer run can never slip in between the re-check and the append
            # (mirrors `_terminalize_crashed`). Skip if the conversation no
            # longer presents as an abandoned gate of the captured generation.
            fresh_state = await self._rt._store.get_state(cid)
            if (
                fresh_state.execution_status not in _GATE_STATES
                or cid in self._rt.running_conversation_ids()
                or self._rt._run_generation.get(cid) != captured_generation
            ):
                return 0  # a newer run/generation now owns it — stale, skip
            transitioned = await self._rt._lifecycle_commands.append_current_run_transition(
                cid,
                [
                    MessageEvent(
                        source=EventSource.ENVIRONMENT,
                        message=LLMMessage(
                            role="user",
                            content=(
                                "⚠️ This run was waiting on your input and was left "
                                "idle, so it's been closed out. Start a new message "
                                "to pick the work back up."
                            ),
                        ),
                    )
                ],
                LifecycleCommandService.build_status(
                    ConversationStatus.STUCK,
                    detail="reaped: abandoned at gate past TTL",
                ),
                expected_statuses=frozenset(_GATE_STATES),
            )
            if transitioned is None:
                return 0
            # A gate-parked run stays PINNED (its resume must keep the same
            # kernel); reaping it to terminal STUCK must therefore release the
            # pin too (finding #3, same class), else an abandoned gated run
            # leaks its kernel pin forever. Generation-guarded: if a newer run
            # reused the pin during the appends above, leave it — that run owns
            # the pin now (exactly like the crash path's unpin).
            self._rt._unpin_if_current_generation(cid, captured_generation)
            _LOG.info("reaped abandoned gate conversation %s", cid)
            return 1
        except Exception:
            _LOG.exception("failed to evaluate abandoned gate conversation %s", cid)
            return 0


class OrphanReconciler:
    """Startup reconciliation of orphaned RUNNING conversations + container sweep."""

    def __init__(self, rt: Any) -> None:
        self._rt = rt

    async def reconcile_orphaned_runs(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        """Startup reconciliation. A conversation whose latest status is RUNNING but
        whose loop died with the previous server process is an ORPHAN: it shows
        'RUNNING' forever in History / the Deep Research read-only view, and its
        sandbox/GPU may have leaked. On boot there are NO live loops, so every
        RUNNING conversation is stale. Mark each PAUSED (resumable) + drop an
        environment note so the user can pick it up. Returns the count reconciled.

        Single-owner ('local') today; extend across owners when auth lands.
        """
        recovered_seals = await self._rt._lifecycle._recover_finalization_journals()
        if recovered_seals:
            _LOG.info("recovered %d interrupted final workspace seal(s)", recovered_seals)

        reconciled = 0
        cursor: str | None = None
        page = 200
        while True:
            ids = await self._rt._store.list_conversations(
                owner_id=owner_id, limit=page, cursor=cursor
            )
            if not ids:
                break
            for cid in ids:
                # One unreadable conversation must never abort server boot.
                with contextlib.suppress(Exception):
                    state = await self._rt._store.get_state(cid)
                    if state.execution_status is ConversationStatus.RUNNING:
                        command = self._rt._lifecycle_commands
                        transitioned = await command.append_current_run_transition(
                            cid,
                            [
                                MessageEvent(
                                    source=EventSource.ENVIRONMENT,
                                    message=LLMMessage(
                                        role="user",
                                        content=(
                                            "⚠️ This run was interrupted when the server restarted, "
                                            "so its sandbox was reclaimed. It's paused — send a "
                                            "message to pick it up (your saved files restore on "
                                            "the "
                                            "next step)."
                                        ),
                                    ),
                                )
                            ],
                            LifecycleCommandService.build_status(
                                ConversationStatus.PAUSED,
                                detail="reconciled: orphaned RUNNING after server restart",
                            ),
                            expected_statuses=frozenset({ConversationStatus.RUNNING}),
                        )
                        if transitioned is not None:
                            reconciled += 1
            if len(ids) < page:
                break
            cursor = str((int(cursor) if cursor else 0) + len(ids))
        if reconciled:
            _LOG.info("reconciled %d orphaned RUNNING conversation(s) on startup", reconciled)

        # Orphan container sweep: destroy backend containers for conversations that are
        # terminal or suspended. These accumulate when sandboxes are not torn down cleanly
        # (transport drops, crashes, etc.) and consume memory/GPU on the sandbox host.
        # The sweep also covers pmx-egr-* egress sidecars (same label).
        _TERMINAL = {
            ConversationStatus.FINISHED,
            ConversationStatus.STUCK,
            ConversationStatus.ERROR,
            ConversationStatus.PAUSED,
            # IDLE = stopped-by-user (the PRIMARY live stop path — bp-12). After a
            # restart its container is unreachable garbage like any other: handles
            # are in-memory and a resume always builds a FRESH instance.
            ConversationStatus.IDLE,
        }
        await self._sweep_orphan_containers(owner_id=owner_id, terminal_statuses=_TERMINAL)

        return reconciled

    async def _sweep_orphan_containers(self, *, owner_id: str, terminal_statuses: set) -> int:
        """Destroy containers for conversations that are terminal or suspended.
        Called from reconcile_orphaned_runs at startup. Returns count destroyed."""
        service = self._rt._sandbox_service_now()
        live_cids = await service.list_live_instances()
        # Decision phase (cheap existence/status reads): collect the set of cids to
        # destroy. Kept serial + suppressed per-cid so one unreadable conversation
        # doesn't abort the sweep. `status_value` is the log string for the
        # terminal-status branch, or None for the no-row branch (which logs nothing,
        # matching the original).
        to_destroy: list[tuple[str, str | None]] = []
        for cid in live_cids:
            with contextlib.suppress(Exception):
                if not await self._rt._store.conversation_exists(cid):
                    to_destroy.append((cid, None))
                    continue
                state = await self._rt._store.get_state(cid)
                if state.execution_status in terminal_statuses:
                    to_destroy.append((cid, state.execution_status.value))

        # Destroy phase: the actual destroy is slow (~4s each on a container backend),
        # so run them CONCURRENTLY with bounded fan-out instead of serially on the
        # awaited startup critical path (~25s → ~4-5s for 6 containers). Order doesn't
        # matter; each task is suppressed so one bad cid never aborts the others.
        sem = asyncio.Semaphore(6)

        async def _destroy_one(cid: str, status_value: str | None) -> bool:
            async with sem:
                with contextlib.suppress(Exception):
                    await service.destroy_by_conversation(cid)
                    if status_value is not None:
                        _LOG.info(
                            "swept orphan container cid=%s status=%s",
                            cid,
                            status_value,
                        )
                    return True
            return False

        results = await asyncio.gather(
            *(_destroy_one(cid, status_value) for cid, status_value in to_destroy)
        )
        destroyed = sum(1 for ok in results if ok)
        # Process backend: sweep workspace dirs older than 7 days (no container layer).
        if isinstance(service, ProcessSandboxService):
            with contextlib.suppress(Exception):
                stale = await service.sweep_stale_workspaces()
                if stale:
                    _LOG.info("swept %d stale process workspace dir(s)", stale)
            # Also sweep orphaned /tmp/pmx-sbx-* root dirs from prior process runs
            # (each ProcessSandboxService.__init__ creates a NEW root via mkdtemp,
            # so roots accumulate across restarts without this cleanup).
            with contextlib.suppress(Exception):
                stale_roots = await service.sweep_stale_roots()
                if stale_roots:
                    _LOG.info("swept %d orphaned pmx-sbx-* root dir(s)", stale_roots)
        if destroyed:
            _LOG.info("swept %d orphan container(s) at startup", destroyed)
        return destroyed


class Rehydration:
    """Workspace durability — restore snapshot files + uploads into a live sandbox."""

    def __init__(self, rt: Any) -> None:
        self._rt = rt

    async def _maybe_rehydrate(self, conversation_id: str) -> None:
        """Restore the workspace files from a prior snapshot into the live
        sandbox, if a snapshot exists. Idempotent: tracked via an in-process
        flag so a second kick on the same conversation doesn't re-write the
        files."""
        if getattr(self._rt, "_rehydrated", None) is None:
            self._rt._rehydrated = set()
        if conversation_id in self._rt._rehydrated:
            return
        self._rt._rehydrated.add(conversation_id)
        store = self._rt._project_store_now()
        if store is None or store.status() != StorageStatus.OK:
            return
        record = None
        try:
            record = store.get(conversation_id)
        except Exception:  # noqa: BLE001 — manifest unreadable: treat as no record
            return
        if record is None or record.files_missing:
            return
        executor = self._rt._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return
        try:
            await rehydrate_workspace(session, store.path_for(conversation_id))
        except Exception as exc:  # noqa: BLE001 — surface, don't crash the run
            await self._rt._emit_persistence_reminder(
                conversation_id,
                f"Could not restore project files: {exc}",
            )

    async def _rehydrate_after_recreate(self, conversation_id: str) -> None:
        """Mid-run recreate (transport drop / OOM-killed box): SandboxSession
        replaced a dead instance with a FRESH one whose workspace is EMPTY — the
        conv_f3bdc842 production incident ("all files were lost"). Clear the
        idempotency flag and rehydrate so the agent's retry lands on its files,
        not a bare dir. _maybe_rehydrate writes through the executor's session,
        which already points at the new instance — no extra plumbing needed.
        Best-effort: with no snapshot yet (first run), there is nothing to
        restore and the agent rebuilds, exactly as before."""
        rehydrated = getattr(self._rt, "_rehydrated", None)
        if rehydrated is not None:
            rehydrated.discard(conversation_id)
        await self._rt._maybe_rehydrate(conversation_id)
        # DC-07: also re-materialize uploaded files.
        await self._rt._rematerialize_uploads(conversation_id)

    async def _rematerialize_uploads(self, conversation_id: str) -> None:
        """[DC-07] Copy server-held uploads back into the fresh sandbox."""
        if not self._rt._uploads_base:
            return

        # We need the session to write files. The executor's session if a loop
        # exists; otherwise the pending session if it's a pre-kick recreation.
        executor = self._rt._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            session = self._rt._pending_sessions.get(conversation_id)

        if session is None:
            return

        uploads_dir = Path(self._rt._uploads_base) / conversation_id
        if not uploads_dir.is_dir():
            return

        # Write them back into the sandbox.
        written = 0
        for p in uploads_dir.iterdir():
            if p.is_file():
                try:
                    data = p.read_bytes()
                    await session.write_file(f"uploads/{p.name}", data)
                    written += 1
                except Exception:  # noqa: BLE001 — best effort
                    _LOG.warning(
                        "[dc-07] failed to re-materialize upload %r for %s",
                        p.name,
                        conversation_id,
                    )
        if written:
            _LOG.info(
                "[dc-07] re-materialized %d upload(s) for %s",
                written,
                conversation_id,
            )


class LifecycleManager:
    """Sandbox lifecycle logic; live runtime state via the back-ref.

    Thin orchestrator over the cohesive private collaborators
    (``WorkspacePersistence``, ``GateReaper``, ``OrphanReconciler``,
    ``Rehydration``). Every callable name/signature is preserved as a one-line
    delegate so the runtime/test seams are undisturbed.
    """

    def __init__(self, rt: Any) -> None:
        self._rt = rt
        self._persistence = WorkspacePersistence(rt)
        self._gate_reaper = GateReaper(rt)
        self._orphan_reconciler = OrphanReconciler(rt)
        self._rehydration = Rehydration(rt)

    async def commit_finished_workspace(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
        *,
        require_inactive_finished_head: bool = False,
    ) -> StatusEvent:
        """Persist FINISHED and its immutable workspace commit as one barrier."""
        return await self._persistence._do_commit_finished_workspace(
            conversation_id,
            terminal_event,
            require_inactive_finished_head=require_inactive_finished_head,
            snapshot_fn=snapshot_workspace,
        )

    async def probe_finish_sealability(self, conversation_id: str) -> SealabilityProbeResult:
        """REL-27 — dry-run the final-seal snapshot; report strict-seal refusals.

        Resolves ``snapshot_workspace`` from this module at call time, exactly
        like ``commit_finished_workspace`` — the probe and the seal share one
        snapshot seam (and one monkeypatch point) by construction.
        """
        return await probe_finish_sealability(
            self._rt,
            conversation_id,
            snapshot_fn=snapshot_workspace,
        )

    async def commit_finished_host_mirror_locked(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
    ) -> StatusEvent:
        """Seal server-owned mirror bytes while the caller holds the workspace lock."""

        return await self._persistence._do_commit_finished_host_mirror_locked(
            conversation_id,
            terminal_event,
        )

    async def _recover_finalization_journals(self) -> int:
        return await self._persistence._do_recover_finalization_journals()

    async def _teardown_sandbox(self, conversation_id: str) -> None:
        """Destroy a conversation's sandbox session (frees the container/port/memory +
        the idle preview server) while KEEPING the event log + the project snapshot. A
        later run re-creates the sandbox and rehydrates. Callers MUST ensure the
        workspace is durable (snapshotted) first — this does not snapshot."""
        # [REL-RC-C] Do ALL synchronous resume-critical state resets UP FRONT — BEFORE any await —
        # then do the best-effort awaited resource reclaim (kill/destroy) LAST. This is both:
        #   (1) POP the three caches (executors/pending/loops) synchronously so a RESUME/re-kick
        #       lands during the awaits below rebuilds a FRESH executor+loop+session via _loop_for
        #       instead of reusing the cached loop bound to the now-_killed executor (which returns
        #       "executor killed; instance revoked" for every call → STUCK — the pause→resume fail);
        #   (2) CANCELLATION-SAFE: on_connect can cancel this suspend task mid-`await`. If the
        #       rehydrate-once discard + cache cleanup ran after the awaits, a cancellation
        #       would skip them and the resumed fresh loop would start in an EMPTY workspace.
        #       Doing them synchronously up front means a cancellation only skips the best-effort
        #       kill/destroy (the container is reaped by the next teardown / idle TTL anyway) — the
        #       resume-critical invariants (fresh rebuild + rehydrate) always hold.
        # Mirrors + strengthens the already-correct control_ops.kill() (control_ops.py:218-224).
        executor = self._rt._executors.pop(conversation_id, None)
        pending = self._rt._pending_sessions.pop(conversation_id, None)
        self._rt._loops.pop(conversation_id, None)  # force a fresh sandbox on the next run
        # F3: clear the read-before-write tracker UNCONDITIONALLY on teardown — the
        # executor's own kill() clears it too, but a teardown where the executor was
        # already popped/absent would otherwise leave the module-global entry behind.
        with contextlib.suppress(Exception):
            from disco.tools.builtin.files import clear_conversation_read_state

            clear_conversation_read_state(conversation_id)
        # BP-14: drop the capture-pane coalescing cache + locks for this conversation —
        # each cache entry pins up to 100KB of captured output and would otherwise
        # accumulate for the life of the server process.
        self._rt._connections.clear_conversation(conversation_id)
        # The sandbox (and its files) are gone, so the NEXT run must rehydrate the
        # snapshot into a fresh sandbox. Clear the rehydrate-once flag — otherwise
        # `_maybe_rehydrate` skips it and the continuation runs in an EMPTY workspace,
        # silently losing all prior work (the "can't keep building after the first
        # plan finished" bug — the second iteration started from nothing).
        rehydrated = getattr(self._rt, "_rehydrated", None)
        if rehydrated is not None:
            rehydrated.discard(conversation_id)
        # Best-effort resource reclaim LAST (safe to cancel — resume state already consistent).
        if executor is not None:
            with contextlib.suppress(Exception):
                await executor.kill()  # destroys the sandbox instance (§6.4)
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.destroy()

    async def reconcile_orphaned_runs(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        return await self._orphan_reconciler.reconcile_orphaned_runs(owner_id=owner_id)

    async def _sweep_orphan_containers(self, *, owner_id: str, terminal_statuses: set) -> int:
        return await self._orphan_reconciler._sweep_orphan_containers(
            owner_id=owner_id, terminal_statuses=terminal_statuses
        )

    def on_connect(self, conversation_id: str) -> None:
        """A UI WebSocket connected — track it and cancel any pending idle-suspend
        (the user is back before the grace elapsed, or the WS reconnected)."""
        self._rt._connections.on_connect(conversation_id)

    def on_disconnect(self, conversation_id: str, *, grace_s: float = 60.0) -> None:
        """A UI WebSocket closed. When the LAST connection for a conversation goes,
        schedule an idle-suspend after `grace_s` — long enough that a brief blip (the
        WS-reconnect backoff) reconnects and cancels it before it fires."""
        self._rt._connections.on_disconnect(conversation_id, grace_s=grace_s)

    async def _suspend_after_grace(self, conversation_id: str, grace_s: float) -> None:
        await self._rt._connections._suspend_after_grace(conversation_id, grace_s)

    def _has_active_work(self, conversation_id: str) -> bool:
        """LIFE-3 — True when a suspend would KILL in-flight work even though the
        durable status is not RUNNING: a live (not-done) run task means the loop is
        mid-turn / mid-tool-call. (A preview server lives INSIDE the sandbox and is
        restored from the snapshot on resume, so it does not by itself block suspend.)
        Disconnect must never destroy active work."""
        task = self._rt._tasks.get(conversation_id)
        return task is not None and not task.done()

    async def _suspend(self, conversation_id: str) -> None:
        """Free an IDLE build's sandbox (its last UI closed): snapshot first, then
        tear down the container/port/memory/preview-server. Skips when there's no
        live sandbox, when storage isn't ready (no durable snapshot → keep the
        sandbox so nothing is lost), when the loop is actively RUNNING, or when there
        is other in-flight work — a live run task (LIFE-3). Resume (or the next
        message) re-creates the sandbox and rehydrates from the snapshot."""
        if conversation_id not in self._rt._executors:
            return
        if self._rt._project_store_now().status() != StorageStatus.OK:
            return
        state = await self._rt._store.get_state(conversation_id)
        if state.execution_status is ConversationStatus.RUNNING:
            return
        if self._has_active_work(conversation_id):
            return
        with contextlib.suppress(Exception):
            async with self._rt.workspace_lock(conversation_id):
                events = await self._rt._store.get_events(conversation_id)
                committed = False
                if state.execution_status is ConversationStatus.FINISHED:
                    try:
                        resolve_committed_workspace(
                            events,
                            self._rt._project_store_now(),
                            conversation_id,
                        )
                        committed = True
                    except WorkspaceCommitUnavailable:
                        pass
                if not committed:
                    await self._rt._maybe_snapshot(conversation_id, trigger="suspend")
                # [REL-RC-C] RE-VALIDATE after the snapshot await — it yields, and a
                # POST /resume (or a new message) can re-kick the cached loop.
                if conversation_id not in self._rt._executors:
                    return
                state = await self._rt._store.get_state(conversation_id)
                if state.execution_status is ConversationStatus.RUNNING or self._has_active_work(
                    conversation_id
                ):
                    return
                await self._rt._teardown_sandbox(conversation_id)
            _LOG.info("auto-suspended idle conversation %s (no UI connected)", conversation_id)

    def sandbox_state(self, conversation_id: str) -> str | None:
        """Return 'active' when a live executor or pending session exists for
        conversation_id. Return 'suspended' when a snapshot record exists (the sandbox
        was torn down but is restorable). Return None when no sandbox context exists
        (research surface / no snapshot)."""
        if conversation_id in self._rt._executors or conversation_id in self._rt._pending_sessions:
            return "active"
        store = self._rt._project_store_now()
        if store is None:
            return None
        try:
            record = store.get(conversation_id)
        except Exception:  # noqa: BLE001 — unreadable manifest: treat as absent
            return None
        return "suspended" if record is not None else None

    def sandbox_instance_ids(self, conversation_id: str) -> list[str]:
        """Live sandbox instance ids for a conversation, without creating or waking one."""
        ids: list[str] = []

        def add(raw: Any) -> None:
            if not isinstance(raw, str):
                return
            sid = raw.strip()
            if not sid or sid.startswith("session-") or sid in ids:
                return
            ids.append(sid)

        executor = self._rt._executors.get(conversation_id)
        if executor is not None:
            add(getattr(getattr(executor, "sandbox", None), "id", None))
        pending = self._rt._pending_sessions.get(conversation_id)
        if pending is not None:
            add(getattr(pending, "id", None))
        return ids

    async def sweep_idle_once(self) -> int:
        """Single idle-TTL sweep pass: suspend all tracked sandboxes that are not
        RUNNING, have no live UI connections, and whose last event is older than
        PMX_IDLE_SUSPEND_S (default 1800 s). Returns the count suspended.

        Exposed so unit tests can drive it directly without sleeping."""
        ttl_env = disco_env("IDLE_SUSPEND_S")
        if ttl_env is not None:
            ttl_s = float(ttl_env)
        else:
            ttl_s = float(self._rt._config_store.load().sandbox.idle_ttl_s)
        suspended = 0
        for cid in list(self._rt._executors):
            with contextlib.suppress(Exception):
                state = await self._rt._store.get_state(cid)
                if state.execution_status is ConversationStatus.RUNNING:
                    continue
                if self._rt._connections.has_connections(cid):
                    continue
                if self._has_active_work(cid):  # LIFE-3: never suspend in-flight work
                    continue
                # Use the last event's timestamp from the store — no parallel clock.
                events = await self._rt._store.get_events(
                    cid,
                    EventFilter(after_seq=state.last_seq - 1) if state.last_seq > 0 else None,
                )
                if not events:
                    continue
                last_ts = events[-1].timestamp
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=UTC)
                idle_s = (datetime.now(tz=UTC) - last_ts).total_seconds()
                if idle_s < ttl_s:
                    continue
                await self._rt._suspend(cid)
                _LOG.info("suspended idle sandbox cid=%s idle_s=%.0f", cid, idle_s)
                suspended += 1
        return suspended

    async def sweep_abandoned_gates_once(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        return await self._gate_reaper.sweep_abandoned_gates_once(owner_id=owner_id)

    async def _sweep_abandoned_gates_logged(self) -> None:
        """Keep the background loop alive while making whole-sweep failures visible."""

        try:
            await self._rt.sweep_abandoned_gates_once()
        except Exception:
            _LOG.exception("abandoned gate sweep failed")

    @staticmethod
    def _idle_sweep_interval_s() -> float:
        raw = disco_env("IDLE_SWEEP_INTERVAL_S", str(_DEFAULT_IDLE_SWEEP_INTERVAL_S))
        assert raw is not None  # default above is non-None
        try:
            interval_s = float(raw)
        except ValueError:
            interval_s = 0.0
        if not math.isfinite(interval_s) or interval_s < _MIN_IDLE_SWEEP_INTERVAL_S:
            _LOG.error(
                "DISCO_IDLE_SWEEP_INTERVAL_S must be finite and at least %.0fs; "
                "using bounded %.0fs default",
                _MIN_IDLE_SWEEP_INTERVAL_S,
                _DEFAULT_IDLE_SWEEP_INTERVAL_S,
            )
            return _DEFAULT_IDLE_SWEEP_INTERVAL_S
        return interval_s

    async def _idle_sweep_loop(self) -> None:
        """Background task: periodically sweep idle sandboxes. Created by the app
        lifespan alongside reconcile_orphaned_runs; cancelled cleanly on shutdown.

        Also reclaims the bundled audio-overview TTS model (RP-09): the in-process
        Kokoro engine stays resident after a synth, so this sweep unloads it once it
        has been idle past its TTL — freeing ~0.5 GB without the user toggling Audio
        off. The bundled Kokoro engine ships in core deps, but the import stays lazy
        and suppressed so a missing/broken native onnxruntime can't wedge startup, and
        a sweep failure never disturbs the sandbox sweep."""
        while True:
            interval_s = self._idle_sweep_interval_s()
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                return
            with contextlib.suppress(Exception):
                await self._rt.sweep_idle_once()
            # W11 backstop: reconcile conversations stranded at RUNNING with no live
            # task (lost done-callback / never re-kicked) so a stall self-heals or
            # becomes visibly STUCK instead of hanging RUNNING forever.
            with contextlib.suppress(Exception):
                await self._rt.sweep_stranded_runs_once()
            # P-C: reap conversations abandoned at an AWAITING_* gate past a long TTL
            # so open gates don't accumulate (the idle sweep only frees their sandbox).
            await self._sweep_abandoned_gates_logged()
            with contextlib.suppress(Exception):
                tts_idle_ttl = disco_env("TTS_IDLE_TTL_S", "1800")
                assert tts_idle_ttl is not None  # default above is non-None
                # maybe_unload_if_idle takes int; env values are integer TTLs.
                ttl_s = int(float(tts_idle_ttl))
                from disco.agent_server import tts_local

                await tts_local.maybe_unload_if_idle(ttl_s=ttl_s)

    async def _maybe_rehydrate(self, conversation_id: str) -> None:
        return await self._rehydration._maybe_rehydrate(conversation_id)

    async def _rehydrate_after_recreate(self, conversation_id: str) -> None:
        return await self._rehydration._rehydrate_after_recreate(conversation_id)

    async def _rematerialize_uploads(self, conversation_id: str) -> None:
        return await self._rehydration._rematerialize_uploads(conversation_id)

    async def _maybe_snapshot(self, conversation_id: str, *, trigger: str = "turn") -> None:
        """Best-effort recovery snapshot for a non-FINISHED boundary.

        PIN the sandbox here, while this boundary still owns the executor.

        `commit_finished_workspace` has pinned since pilot seed 406546, because
        by capture time the executor may already be gone. Every OTHER ended state
        re-resolved it inside `_resolve_capture_session` and lost the race —
        certified-lane seed 621005 (2026-07-27) ended after 1104s with no
        `<projects_root>/<cid>/` at all: no tree, no versions, no manifest.

        The window is already known here: `_suspend` re-validates
        `conversation_id not in self._rt._executors` immediately AFTER this
        await, because the executor can vanish across it. The snapshot simply was
        not given the same protection.

        Holding the reference does not keep a dead box alive — capture still
        fails closed when the session is gone, and `_resolve_capture_session`
        still prefers the live session when the pin turns out to be a previous
        generation. It removes the WINDOW, nothing more.
        """

        # DECLINE when this terminal is already sealed. A FINISHED run is captured
        # and sealed synchronously by the loop's terminal hook and THEN rides the
        # idle sweep into `_suspend`, which lands here. Its workspace is already
        # immutably persisted, so a recovery capture adds nothing — and cutting a
        # `trigger="suspend"` version AFTER the terminal displaces the finish
        # marker for any reader that takes the latest version event.
        #
        # cert9 F1 2026-07-28: `WORKSPACE_SNAPSHOT_NOT_READY` /
        # "workspace version is not finish-triggered". Before the F-21 pin this
        # capture was skipped (no executor), cut no version, and the seal stayed
        # latest; with the pin it succeeds. The pin is right — this is the
        # redundant capture it exposed.
        try:
            resolve_committed_workspace(
                await self._rt._store.get_events(conversation_id),
                self._rt._project_store_now(),
                conversation_id,
            )
        except WorkspaceCommitUnavailable:
            pass  # no sealed terminal — this recovery capture is the real one
        else:
            return

        executor = self._rt._executors.get(conversation_id)
        await self._capture_workspace(
            conversation_id,
            trigger=trigger,
            pinned_session=getattr(executor, "_sandbox", None) if executor is not None else None,
        )

    async def _capture_workspace(
        self,
        conversation_id: str,
        *,
        trigger: str,
        version_label: str = "",
        seal_fence: tuple[int, int | None] | None = None,
        journal: dict[str, Any] | None = None,
        pinned_session: Any | None = None,
    ) -> WorkspaceVersionEvent | None:
        """Mirror live bytes and optionally publish a strict immutable seal."""
        return await self._persistence._do_capture_workspace(
            conversation_id,
            trigger=trigger,
            version_label=version_label,
            seal_fence=seal_fence,
            pinned_session=pinned_session,
            journal=journal,
            snapshot_fn=snapshot_workspace,
        )

    async def _maybe_synthesize_app_deliverable(
        self, conversation_id: str, snapshot_dir: Path
    ) -> None:
        """Synthesize an app deliverable for an index.html without a serve event."""
        return await self._persistence._do_maybe_synthesize_app_deliverable(
            conversation_id, snapshot_dir
        )

    @staticmethod
    def _find_snapshot_index(snapshot_dir: Path) -> Path | None:
        """First index.html in the snapshot (root preferred, else shallowest subdir)."""
        return WorkspacePersistence._find_snapshot_index(snapshot_dir)
