"""Sandbox lifecycle — auto-suspend, idle sweep, orphan reconcile, snapshot/
rehydrate — extracted from `runtime.py`.

The build-sandbox lifecycle machinery is owned by ``LifecycleManager`` and its
cohesive collaborators (``GateReaper``, ``OrphanReconciler``, ``Rehydration``).
Each collaborator receives explicit typed named owners (ports) — never a
whole-runtime back-reference, ``Any``, or a service locator.

  - auto-suspend (lifecycle G): on_connect / on_disconnect / _suspend_after_grace
    / _suspend / sandbox_state
  - idle TTL sweep: sweep_idle_once / _idle_sweep_loop
  - sandbox teardown + startup orphan reconcile: _teardown_sandbox /
    reconcile_orphaned_runs / _sweep_orphan_containers
  - workspace durability: _maybe_rehydrate / _rehydrate_after_recreate /
    _rematerialize_uploads / _maybe_snapshot
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
    snapshot_workspace,
)

from .lifecycle_command_service import LifecycleCommandService
from .lifecycle_ports import (
    LifecycleConnections,
    LifecycleIdleSweepDeps,
    LifecycleKernelPins,
    LifecyclePersistenceOwner,
    LifecycleRunState,
    LifecycleSandboxAccess,
    LifecycleStoreAccess,
    WorkspaceSealProbe,
)
from .lifecycle_rehydration import Rehydration
from .workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace
from .workspace_fence import WorkspaceFenceService

_LOG = logging.getLogger(__name__)


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

    def __init__(
        self,
        store: LifecycleStoreAccess,
        run_state: LifecycleRunState,
        connections: LifecycleConnections,
        commands: LifecycleCommandService,
        kernel_pins: LifecycleKernelPins,
    ) -> None:
        self._store = store
        self._run_state = run_state
        self._connections = connections
        self._commands = commands
        self._kernel_pins = kernel_pins

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
        live_run_ids = set(self._run_state.active_conversation_ids())
        reaped = 0
        cursor: str | None = None
        page = 200
        while True:
            ids = await self._store.list_conversations(owner_id=owner_id, limit=page, cursor=cursor)
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
            state = await self._store.get_state(cid)
            if state.execution_status not in _GATE_STATES:
                return 0
            if cid in live_run_ids:
                return 0  # a live in-memory run/task is driving it — not abandoned
            if self._connections.has_connections(cid):
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
            captured_generation = self._run_state.generation(cid)
            events = await self._store.get_events(
                cid,
                event_filter=EventFilter(after_seq=state.last_seq - 1)
                if state.last_seq > 0
                else None,
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
            fresh_state = await self._store.get_state(cid)
            if (
                fresh_state.execution_status not in _GATE_STATES
                or cid in self._run_state.active_conversation_ids()
                or not self._run_state.generation_is_current(
                    cid,
                    captured_generation,
                )
            ):
                return 0  # a newer run/generation now owns it — stale, skip
            transitioned = await self._commands.append_current_run_transition(
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
            self._kernel_pins.clear_if_current(
                cid,
                captured_generation,
            )
            _LOG.info("reaped abandoned gate conversation %s", cid)
            return 1
        except Exception:
            _LOG.exception("failed to evaluate abandoned gate conversation %s", cid)
            return 0


class OrphanReconciler:
    """Startup reconciliation of orphaned RUNNING conversations + container sweep."""

    def __init__(
        self,
        store: LifecycleStoreAccess,
        sandbox: LifecycleSandboxAccess,
        commands: LifecycleCommandService,
        persistence: LifecyclePersistenceOwner,
    ) -> None:
        self._store = store
        self._sandbox = sandbox
        self._commands = commands
        self._persistence = persistence

    async def reconcile_orphaned_runs(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        """Startup reconciliation. A conversation whose latest status is RUNNING but
        whose loop died with the previous server process is an ORPHAN: it shows
        'RUNNING' forever in History / the Deep Research read-only view, and its
        sandbox/GPU may have leaked. On boot there are NO live loops, so every
        RUNNING conversation is stale. Mark each PAUSED (resumable) + drop an
        environment note so the user can pick it up. Returns the count reconciled.

        Single-owner ('local') today; extend across owners when auth lands.
        """
        recovered_seals = await self._persistence.recover_finalization_journals()
        if recovered_seals:
            _LOG.info("recovered %d interrupted final workspace seal(s)", recovered_seals)

        reconciled = 0
        cursor: str | None = None
        page = 200
        while True:
            ids = await self._store.list_conversations(owner_id=owner_id, limit=page, cursor=cursor)
            if not ids:
                break
            for cid in ids:
                # One unreadable conversation must never abort server boot.
                with contextlib.suppress(Exception):
                    state = await self._store.get_state(cid)
                    if state.execution_status is ConversationStatus.RUNNING:
                        transitioned = await self._commands.append_current_run_transition(
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
        service = self._sandbox.sandbox_service_now()
        live_cids = await service.list_live_instances()
        # Decision phase (cheap existence/status reads): collect the set of cids to
        # destroy. Kept serial + suppressed per-cid so one unreadable conversation
        # doesn't abort the sweep. Only a conversation positively owned by this
        # runtime can authorize destruction; absent and foreign-owned rows are
        # retained even when their status is terminal.
        to_destroy: list[tuple[str, str | None]] = []
        for cid in live_cids:
            with contextlib.suppress(Exception):
                if not await self._store.conversation_owned_by(cid, owner_id):
                    continue
                state = await self._store.get_state(cid)
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


class LifecycleManager:
    """Sandbox lifecycle logic; live state through explicit typed named owners.

    Thin orchestrator over the cohesive private collaborators
    (``LifecyclePersistenceOwner``, ``GateReaper``, ``OrphanReconciler``,
    ``Rehydration``). Every callable name/signature is preserved as a one-line
    delegate so the runtime/test seams are undisturbed.
    """

    def __init__(
        self,
        persistence: LifecyclePersistenceOwner,
        gate_reaper: GateReaper,
        orphan_reconciler: OrphanReconciler,
        rehydration: Rehydration,
        store: LifecycleStoreAccess,
        run_state: LifecycleRunState,
        connections: LifecycleConnections,
        sandbox: LifecycleSandboxAccess,
        idle_sweep_deps: LifecycleIdleSweepDeps,
        workspace: WorkspaceFenceService,
    ) -> None:
        self._persistence = persistence
        self._gate_reaper = gate_reaper
        self._orphan_reconciler = orphan_reconciler
        self._rehydration = rehydration
        self._store = store
        self._run_state = run_state
        self._connections = connections
        self._sandbox = sandbox
        self._idle_sweep_deps = idle_sweep_deps
        self._workspace = workspace
        self._seal_probe = WorkspaceSealProbe(run_state._resources_registry())

    async def commit_finished_workspace(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
        *,
        require_inactive_finished_head: bool = False,
    ) -> StatusEvent:
        """Persist FINISHED and its immutable workspace commit as one barrier."""
        return await self._persistence.commit_finished_workspace(
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
        return await self._seal_probe.probe(
            conversation_id,
            snapshot_fn=snapshot_workspace,
        )

    async def commit_finished_host_mirror_locked(
        self,
        conversation_id: str,
        terminal_event: StatusEvent,
    ) -> StatusEvent:
        """Seal server-owned mirror bytes while the caller holds the workspace lock."""

        return await self._persistence.commit_finished_host_mirror_locked(
            conversation_id,
            terminal_event,
        )

    async def _recover_finalization_journals(self) -> int:
        return await self._persistence.recover_finalization_journals()

    def _detach_sandbox(self, conversation_id: str) -> tuple[Any, Any]:
        """Synchronous detach/reset phase — no awaits.

        Pops executor and pending session, forgets the loop, and clears session
        markers before any await. Returns the detached old resources for the
        async reclaim phase to kill/destroy. All resume-critical cache resets
        happen here, so cancellation during reclaim never leaves the resume
        path in an inconsistent state.
        """
        executor = self._run_state.pop_executor(conversation_id)
        pending = self._run_state.pop_pending_session(conversation_id)
        self._run_state.forget(conversation_id)  # force a fresh sandbox on the next run
        self.clear_session_markers(conversation_id)
        return executor, pending

    async def _reclaim_detached_sandbox(self, executor: Any, pending: Any) -> None:
        """Async best-effort reclaim of detached old resources only."""
        if executor is not None:
            with contextlib.suppress(Exception):
                await executor.kill()  # destroys the sandbox instance (§6.4)
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.destroy()

    def _track_detached_reclaim(
        self,
        conversation_id: str,
        executor: Any,
        pending: Any,
    ) -> asyncio.Task[None]:
        """Publish detached ownership before releasing the workspace fence."""

        return self._run_state.track_reclaim(
            conversation_id,
            self._reclaim_detached_sandbox(executor, pending),
        )

    async def _teardown_sandbox(self, conversation_id: str) -> None:
        """Explicit kill/delete overrides a pending inter-request preview intent."""
        async with self._connections.preview_capture_lock(conversation_id):
            await self._teardown_sandbox_with_preview_ownership(conversation_id)

    async def _teardown_sandbox_with_preview_ownership(self, conversation_id: str) -> None:
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
        executor, pending = self._detach_sandbox(conversation_id)
        self._track_detached_reclaim(conversation_id, executor, pending)
        await self._run_state.await_reclaims(conversation_id)

    def clear_session_markers(self, conversation_id: str) -> None:
        """Reset every cache that must not survive sandbox replacement."""
        with contextlib.suppress(Exception):
            from disco.tools.builtin.files import clear_conversation_read_state

            clear_conversation_read_state(conversation_id)
        # BP-14: drop the capture-pane coalescing cache + locks for this conversation —
        # each cache entry pins up to 100KB of captured output and would otherwise
        # accumulate for the life of the server process.
        self._connections.clear_session_state(conversation_id)
        # The sandbox (and its files) are gone, so the NEXT run must rehydrate the
        # snapshot into a fresh sandbox. Clear the rehydrate-once flag — otherwise
        # `_maybe_rehydrate` skips it and the continuation runs in an EMPTY workspace,
        # silently losing all prior work (the "can't keep building after the first
        # plan finished" bug — the second iteration started from nothing).
        self._rehydration.clear(conversation_id)

    async def reconcile_orphaned_runs(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        return await self._orphan_reconciler.reconcile_orphaned_runs(owner_id=owner_id)

    async def _sweep_orphan_containers(self, *, owner_id: str, terminal_statuses: set) -> int:
        return await self._orphan_reconciler._sweep_orphan_containers(
            owner_id=owner_id, terminal_statuses=terminal_statuses
        )

    def _has_active_work(self, conversation_id: str) -> bool:
        """LIFE-3 — True when a suspend would KILL in-flight work even though the
        durable status is not RUNNING: a live (not-done) run task means the loop is
        mid-turn / mid-tool-call. (A preview server lives INSIDE the sandbox and is
        restored from the snapshot on resume, so it does not by itself block suspend.)
        Disconnect must never destroy active work."""
        return self._run_state.active_task(conversation_id) is not None

    async def _suspend(self, conversation_id: str) -> None:
        """Suspend only after any in-flight preview capture releases ownership."""
        if self._connections.preview_capture_active(conversation_id):
            return
        async with self._connections.preview_capture_lock(conversation_id):
            if self._connections.preview_capture_active(conversation_id):
                return
            await self._suspend_with_preview_ownership(conversation_id)

    async def _suspend_with_preview_ownership(self, conversation_id: str) -> None:
        """Free an IDLE build's sandbox (its last UI closed): snapshot first, then
        tear down the container/port/memory/preview-server. Skips when there's no
        live sandbox, when storage isn't ready (no durable snapshot → keep the
        sandbox so nothing is lost), when the loop is actively RUNNING, or when there
        is other in-flight work — a live run task (LIFE-3). Resume (or the next
        message) re-creates the sandbox and rehydrates from the snapshot."""
        if not self._run_state.has_executor(conversation_id):
            return
        with contextlib.suppress(Exception):
            async with self._workspace.lock(conversation_id):
                if not self._run_state.has_executor(conversation_id):
                    return
                if self._sandbox.current_project_store().status() != StorageStatus.OK:
                    return
                state = await self._store.get_state(conversation_id)
                if state.execution_status is ConversationStatus.RUNNING:
                    return
                if self._has_active_work(conversation_id):
                    return
                events = await self._store.get_events(conversation_id)
                committed = False
                if state.execution_status is ConversationStatus.FINISHED:
                    try:
                        resolve_committed_workspace(
                            events,
                            self._sandbox.current_project_store(),
                            conversation_id,
                        )
                        committed = True
                    except WorkspaceCommitUnavailable:
                        pass
                if not committed:
                    await self._maybe_snapshot(conversation_id, trigger="suspend")
                # [REL-RC-C] RE-VALIDATE after the snapshot await — it yields, and a
                # POST /resume (or a new message) can re-kick the cached loop.
                if not self._run_state.has_executor(conversation_id):
                    return
                if self._sandbox.current_project_store().status() != StorageStatus.OK:
                    return
                state = await self._store.get_state(conversation_id)
                if state.execution_status is ConversationStatus.RUNNING or self._has_active_work(
                    conversation_id
                ):
                    return
                executor, pending = self._detach_sandbox(conversation_id)
                self._track_detached_reclaim(conversation_id, executor, pending)
            await self._run_state.await_reclaims(conversation_id)
            _LOG.info("auto-suspended idle conversation %s (no UI connected)", conversation_id)

    def sandbox_state(self, conversation_id: str) -> str | None:
        """Return 'active' when a live executor or pending session exists for
        conversation_id. Return 'suspended' when a snapshot record exists (the sandbox
        was torn down but is restorable). Return None when no sandbox context exists
        (research surface / no snapshot)."""
        if self._run_state.has_executor(conversation_id) or self._run_state.has_pending_session(
            conversation_id
        ):
            return "active"
        store = self._sandbox.current_project_store()
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

        executor = self._run_state.executor(conversation_id)
        if executor is not None:
            add(getattr(getattr(executor, "sandbox", None), "id", None))
        pending = self._run_state.pending_session(conversation_id)
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
            ttl_s = self._idle_sweep_deps.idle_ttl_s()
        suspended = 0
        for cid in self._run_state.conversation_ids(executors_only=True):
            with contextlib.suppress(Exception):
                state = await self._store.get_state(cid)
                if state.execution_status is ConversationStatus.RUNNING:
                    continue
                if self._connections.has_connections(cid):
                    continue
                if self._has_active_work(cid):  # LIFE-3: never suspend in-flight work
                    continue
                # Use the last event's timestamp from the store — no parallel clock.
                events = await self._store.get_events(
                    cid,
                    event_filter=EventFilter(after_seq=state.last_seq - 1)
                    if state.last_seq > 0
                    else None,
                )
                if not events:
                    continue
                last_ts = events[-1].timestamp
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=UTC)
                idle_s = (datetime.now(tz=UTC) - last_ts).total_seconds()
                if idle_s < ttl_s:
                    continue
                await self._suspend(cid)
                _LOG.info("suspended idle sandbox cid=%s idle_s=%.0f", cid, idle_s)
                suspended += 1
        return suspended

    async def sweep_abandoned_gates_once(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        return await self._gate_reaper.sweep_abandoned_gates_once(owner_id=owner_id)

    async def _maybe_rehydrate(self, conversation_id: str) -> None:
        return await self._rehydration._maybe_rehydrate(conversation_id)

    async def _rehydrate_after_recreate(self, conversation_id: str) -> None:
        return await self._rehydration._rehydrate_after_recreate(conversation_id)

    async def _rematerialize_uploads(self, conversation_id: str) -> None:
        return await self._rehydration._rematerialize_uploads(conversation_id)

    def _mark_rehydrated(self, conversation_id: str) -> None:
        self._rehydration._mark_rehydrated(conversation_id)

    async def _maybe_snapshot(self, conversation_id: str, *, trigger: str = "turn") -> None:
        """Best-effort recovery snapshot for a non-FINISHED boundary.

        PIN the sandbox here, while this boundary still owns the executor.

        `commit_finished_workspace` has pinned since pilot seed 406546, because
        by capture time the executor may already be gone. Every OTHER ended state
        re-resolved it inside `_resolve_capture_session` and lost the race —
        certified-lane seed 621005 (2026-07-27) ended after 1104s with no
        `<projects_root>/<cid>/` at all: no tree, no versions, no manifest.

        The window is already known here: `_suspend` re-validates
        `has_executor` immediately AFTER this await, because the executor can
        vanish across it. The snapshot simply was not given the same protection.

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
                await self._store.get_events(conversation_id),
                self._sandbox.current_project_store(),
                conversation_id,
            )
        except WorkspaceCommitUnavailable:
            pass  # no sealed terminal — this recovery capture is the real one
        else:
            return

        executor = self._run_state.executor(conversation_id)
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
        return await self._persistence.capture_workspace(
            conversation_id,
            trigger=trigger,
            version_label=version_label,
            seal_fence=seal_fence,
            journal=journal,
            pinned_session=pinned_session,
            snapshot_fn=snapshot_workspace,
        )

    async def _maybe_synthesize_app_deliverable(
        self, conversation_id: str, snapshot_dir: Path
    ) -> None:
        """Synthesize an app deliverable for an index.html without a serve event."""
        return await self._persistence.maybe_synthesize_app_deliverable(
            conversation_id, snapshot_dir
        )

    @staticmethod
    def _find_snapshot_index(snapshot_dir: Path) -> Path | None:
        """First index.html in the snapshot (root preferred, else shallowest subdir)."""
        return LifecyclePersistenceOwner.find_snapshot_index(snapshot_dir)
