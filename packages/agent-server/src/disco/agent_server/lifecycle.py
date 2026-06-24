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

`LifecycleManager` reaches the runtime's live state (`_connections`,
`_suspend_tasks`, `_executors`, `_pending_sessions`, `_loops`, `_store`,
`_config_store`, `_uploads_base`, `_rehydrated`, the session-view caches, and
the `_project_store_now` / `_sandbox_service_now` / `_emit_persistence_reminder`
resolvers) via a back-reference — the test-suite and the app's WS handlers
read several of these directly on the runtime. Every method called externally
(app lifespan / routes) or directly by a test stays reachable on
`ConversationRuntime` as a one-line delegator; only the two internal helpers
(`_suspend_after_grace`, `_sweep_orphan_containers`) move without a delegator.
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
    DeliverableEvent,
    EventFilter,
    EventSource,
    LLMMessage,
    MessageEvent,
    StatusEvent,
)
from disco.core.env import disco_env
from disco.tools import ProcessSandboxService
from disco.tools.projects import (
    StorageStatus,
    rehydrate_workspace,
    snapshot_workspace,
)

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


class LifecycleManager:
    """Sandbox lifecycle logic; live runtime state via the back-ref."""

    def __init__(self, rt: Any) -> None:
        self._rt = rt

    async def _teardown_sandbox(self, conversation_id: str) -> None:
        """Destroy a conversation's sandbox session (frees the container/port/memory +
        the idle preview server) while KEEPING the event log + the project snapshot. A
        later run re-creates the sandbox and rehydrates. Callers MUST ensure the
        workspace is durable (snapshotted) first — this does not snapshot."""
        executor = self._rt._executors.pop(conversation_id, None)
        if executor is not None:
            with contextlib.suppress(Exception):
                await executor.kill()  # destroys the sandbox instance (§6.4)
        # F3: clear the read-before-write tracker UNCONDITIONALLY on teardown — the
        # executor's own kill() clears it too, but a teardown where the executor was
        # already popped/absent would otherwise leave the module-global entry behind.
        with contextlib.suppress(Exception):
            from disco.tools.builtin.files import clear_conversation_read_state

            clear_conversation_read_state(conversation_id)
        pending = self._rt._pending_sessions.pop(conversation_id, None)
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.destroy()
        self._rt._loops.pop(conversation_id, None)  # force a fresh sandbox on the next run
        # BP-14: drop the capture-pane coalescing cache + locks for this conversation —
        # each cache entry pins up to 100KB of captured output and would otherwise
        # accumulate for the life of the server process.
        for key in [k for k in self._rt._session_view_cache if k[0] == conversation_id]:
            del self._rt._session_view_cache[key]
        for key in [k for k in self._rt._session_view_locks if k[0] == conversation_id]:
            del self._rt._session_view_locks[key]
        self._rt._wake_locks.pop(conversation_id, None)
        self._rt._last_sessions.pop(conversation_id, None)  # don't ghost a stale list (DC-04b)
        # The sandbox (and its files) are gone, so the NEXT run must rehydrate the
        # snapshot into a fresh sandbox. Clear the rehydrate-once flag — otherwise
        # `_maybe_rehydrate` skips it and the continuation runs in an EMPTY workspace,
        # silently losing all prior work (the "can't keep building after the first
        # plan finished" bug — the second iteration started from nothing).
        rehydrated = getattr(self._rt, "_rehydrated", None)
        if rehydrated is not None:
            rehydrated.discard(conversation_id)

    async def reconcile_orphaned_runs(self, *, owner_id: str = DEFAULT_OWNER_ID) -> int:
        """Startup reconciliation. A conversation whose latest status is RUNNING but
        whose loop died with the previous server process is an ORPHAN: it shows
        'RUNNING' forever in History / the Deep Research read-only view, and its
        sandbox/GPU may have leaked. On boot there are NO live loops, so every
        RUNNING conversation is stale. Mark each PAUSED (resumable) + drop an
        environment note so the user can pick it up. Returns the count reconciled.

        Single-owner ('local') today; extend across owners when auth lands.
        """
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
                        await self._rt._store.append(
                            cid,
                            MessageEvent(
                                source=EventSource.ENVIRONMENT,
                                message=LLMMessage(
                                    role="user",
                                    content=(
                                        "⚠️ This run was interrupted when the server restarted, "
                                        "so its sandbox was reclaimed. It's paused — send a "
                                        "message to pick it up (your saved files restore on the "
                                        "next step)."
                                    ),
                                ),
                            ),
                        )
                        await self._rt._store.append(
                            cid,
                            StatusEvent(
                                status=ConversationStatus.PAUSED,
                                detail="reconciled: orphaned RUNNING after server restart",
                            ),
                        )
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

    async def _sweep_orphan_containers(
        self,
        *,
        owner_id: str,
        terminal_statuses: set,
    ) -> int:
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

    def on_connect(self, conversation_id: str) -> None:
        """A UI WebSocket connected — track it and cancel any pending idle-suspend
        (the user is back before the grace elapsed, or the WS reconnected)."""
        self._rt._connections[conversation_id] = self._rt._connections.get(conversation_id, 0) + 1
        task = self._rt._suspend_tasks.pop(conversation_id, None)
        if task is not None:
            task.cancel()

    def on_disconnect(self, conversation_id: str, *, grace_s: float = 60.0) -> None:
        """A UI WebSocket closed. When the LAST connection for a conversation goes,
        schedule an idle-suspend after `grace_s` — long enough that a brief blip (the
        WS-reconnect backoff) reconnects and cancels it before it fires."""
        n = self._rt._connections.get(conversation_id, 0) - 1
        if n > 0:
            self._rt._connections[conversation_id] = n
            return
        self._rt._connections.pop(conversation_id, None)
        old = self._rt._suspend_tasks.pop(conversation_id, None)
        if old is not None:
            old.cancel()
        self._rt._suspend_tasks[conversation_id] = asyncio.create_task(
            self._suspend_after_grace(conversation_id, grace_s)
        )

    async def _suspend_after_grace(self, conversation_id: str, grace_s: float) -> None:
        try:
            await asyncio.sleep(grace_s)
        except asyncio.CancelledError:
            return
        if self._rt._connections.get(conversation_id, 0) <= 0:
            await self._rt._suspend(conversation_id)
        self._rt._suspend_tasks.pop(conversation_id, None)

    async def _suspend(self, conversation_id: str) -> None:
        """Free an IDLE build's sandbox (its last UI closed): snapshot first, then
        tear down the container/port/memory/preview-server. Skips when there's no
        live sandbox, when storage isn't ready (no durable snapshot → keep the
        sandbox so nothing is lost), or when the loop is actively RUNNING (don't
        interrupt in-flight work — that run continues in the background). Resume (or
        the next message) re-creates the sandbox and rehydrates from the snapshot."""
        if conversation_id not in self._rt._executors:
            return
        if self._rt._project_store_now().status() != StorageStatus.OK:
            return
        state = await self._rt._store.get_state(conversation_id)
        if state.execution_status is ConversationStatus.RUNNING:
            return
        with contextlib.suppress(Exception):
            await self._rt._maybe_snapshot(conversation_id)
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
                if self._rt._connections.get(cid, 0) > 0:
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

    async def sweep_abandoned_gates_once(
        self, *, owner_id: str = DEFAULT_OWNER_ID
    ) -> int:
        """P-C: reap conversations parked at an AWAITING_* gate that the user never
        answered. The idle sweep only frees the SANDBOX of a gated conversation —
        it never resolves the gate, so a plan/decision/question the user walked away
        from lingers as an open gate forever (it shows as "waiting for you" in
        History and its terminal-cleanup never runs). After a GENEROUS TTL
        (DISCO_ABANDONED_GATE_TTL_S, default 24 h) of no new events, route the
        gate to STUCK + a note, so it terminalizes (the orphan-container sweep then
        reclaims any leftover container) instead of accumulating.

        Conservative by construction — a gate is reaped ONLY when:
          • its latest status is one of the AWAITING_* / WAITING_FOR_CONFIRMATION
            gate states (never a RUNNING / already-terminal conversation), AND
          • there is NO live in-memory run/task for it (a just-resumed gate, a
            decision in flight, or any background run is the GROUND TRUTH of
            "executing right now" — cached gate status can lag a live run, so
            reaping on status alone could corrupt an active conversation), AND
          • no UI is currently connected (someone watching it is not abandonment),
            AND
          • its last event is older than the long TTL.
        Returns the count reaped. Single-owner ('local') today, like
        reconcile_orphaned_runs; extend across owners when auth lands."""
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
                # One unreadable conversation must never abort the sweep.
                with contextlib.suppress(Exception):
                    state = await self._rt._store.get_state(cid)
                    if state.execution_status not in _GATE_STATES:
                        continue
                    if cid in live_run_ids:
                        continue  # a live in-memory run/task is driving it — not abandoned
                    if self._rt._connections.get(cid, 0) > 0:
                        continue  # UI attached — not abandoned
                    events = await self._rt._store.get_events(
                        cid,
                        EventFilter(after_seq=state.last_seq - 1)
                        if state.last_seq > 0
                        else None,
                    )
                    if not events:
                        continue
                    last_ts = events[-1].timestamp
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=UTC)
                    if (now - last_ts).total_seconds() < ttl_s:
                        continue
                    await self._rt._store.append(
                        cid,
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
                        ),
                    )
                    await self._rt._store.append(
                        cid,
                        StatusEvent(
                            status=ConversationStatus.STUCK,
                            detail="reaped: abandoned at gate past TTL",
                        ),
                    )
                    # A gate-parked run stays PINNED (its resume must keep the same
                    # kernel); reaping it to terminal STUCK must therefore release the
                    # pin too (finding #3, same class), else an abandoned gated run
                    # leaks its kernel pin forever.
                    self._rt._clear_pinned_kernel(cid)
                    _LOG.info("reaped abandoned gate conversation %s", cid)
                    reaped += 1
            if len(ids) < page:
                break
            cursor = str((int(cursor) if cursor else 0) + len(ids))
        if reaped:
            _LOG.info("reaped %d abandoned gate conversation(s)", reaped)
        return reaped

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
            sweep_interval = disco_env("IDLE_SWEEP_INTERVAL_S", "60")
            assert sweep_interval is not None  # default above is non-None
            interval_s = float(sweep_interval)
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
            with contextlib.suppress(Exception):
                await self._rt.sweep_abandoned_gates_once()
            with contextlib.suppress(Exception):
                tts_idle_ttl = disco_env("TTS_IDLE_TTL_S", "1800")
                assert tts_idle_ttl is not None  # default above is non-None
                # maybe_unload_if_idle takes int; env values are integer TTLs.
                ttl_s = int(float(tts_idle_ttl))
                from disco.agent_server import tts_local

                await tts_local.maybe_unload_if_idle(ttl_s=ttl_s)

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
                        p.name, conversation_id,
                    )
        if written:
            _LOG.info(
                "[dc-07] re-materialized %d upload(s) for %s",
                written, conversation_id,
            )

    async def _maybe_snapshot(self, conversation_id: str) -> None:
        """Mirror the live workspace out to disk + update the manifest."""
        store = self._rt._project_store_now()
        if store is None:
            return
        status = store.status()
        if status != StorageStatus.OK:
            await self._rt._emit_persistence_reminder(
                conversation_id,
                f"project storage is {status.value}; this build was NOT saved.",
            )
            return
        executor = self._rt._executors.get(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return
        # Title pulled from the conversations table; created_at is the row's
        # creation timestamp. Both are cheap reads we surface in the list view.
        title: str | None = None
        created_at: str | None = None
        owner_id: str | None = None
        try:
            summaries = await self._rt._store.list_conversation_summaries(
                owner_id=DEFAULT_OWNER_ID, limit=500, cursor=None
            )
            row = next((s for s in summaries if s.conversation_id == conversation_id), None)
            if row is not None:
                title = row.title
                created_at = row.created_at
                owner_id = row.owner_id
        except Exception:  # noqa: BLE001 — metadata is best-effort
            pass
        try:
            result = await snapshot_workspace(session, store.path_for(conversation_id))
            store.write_manifest(
                conversation_id,
                title=title,
                owner_id=owner_id,
                created_at=created_at,
                file_count=result.file_count,
                total_bytes=result.total_bytes,
            )
        except Exception as exc:  # noqa: BLE001 — surface, don't crash
            await self._rt._emit_persistence_reminder(
                conversation_id,
                f"snapshot failed: {exc}",
            )
            return
        # Fix 2 (B-H.1): a shell-served / npm-built site emits NO app DeliverableEvent
        # (handle_serve is the only emitter; `python3 -m http.server` / a `npm run
        # build` write index.html with no serve tool-call) — so the user gets no
        # "Open app" card and the snapshot serve path is never advertised. If the
        # snapshot has an index.html and no app-deliverable was emitted, synthesize
        # one THROUGH THE EVENT STORE. Idempotent: gated on no existing app-deliverable
        # so a second snapshot / a real serve-emitted card never duplicates it.
        await self._maybe_synthesize_app_deliverable(
            conversation_id, store.path_for(conversation_id)
        )

    async def _maybe_synthesize_app_deliverable(
        self, conversation_id: str, snapshot_dir: Path
    ) -> None:
        """Fix 2 (B-H.1): append a synthetic app DeliverableEvent for a shell-served
        build (index.html on disk, no serve tool-call). Idempotent + best-effort —
        a missing index.html, an existing app-deliverable, or any read/append failure
        is a silent no-op (the snapshot itself already succeeded)."""
        try:
            index = self._find_snapshot_index(snapshot_dir)
            if index is None:
                return
            existing = await self._rt._store.get_events(conversation_id)
            if any(
                isinstance(e, DeliverableEvent) and e.artifact_kind == "app"
                for e in existing
            ):
                return  # codex P1b: idempotency read goes through the store
            rel_dir = index.parent.relative_to(snapshot_dir).as_posix()
            path = rel_dir if rel_dir and rel_dir != "." else "."
            await self._rt._store.append(
                conversation_id,
                DeliverableEvent(
                    source=EventSource.AGENT,
                    title="Web app",
                    path=path,
                    artifact_kind="app",
                    deployment_url="",
                ),
            )
        except Exception:  # noqa: BLE001 — synthetic card is a convenience, never crash snapshot
            _LOG.debug(
                "synthetic app-deliverable skipped for %s", conversation_id, exc_info=True
            )

    @staticmethod
    def _find_snapshot_index(snapshot_dir: Path) -> Path | None:
        """First index.html in the snapshot (root preferred, else shallowest subdir),
        skipping internal dirs — mirrors SandboxSession._detect_serve_dir's skip set."""
        root = snapshot_dir / "index.html"
        if root.is_file():
            return root
        candidates = [
            p for p in snapshot_dir.rglob("index.html")
            if p.is_file()
            and ".pmx" not in p.parts
            and ".disco" not in p.parts
            and "node_modules" not in p.parts
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda p: len(p.parts))
