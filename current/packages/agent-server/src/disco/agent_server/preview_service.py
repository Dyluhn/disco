"""Live-preview proxy + sandbox wake — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The preview-facing
accessors move out of runtime.py into a `PreviewService` collaborator
constructed once in `ConversationRuntime`: cid-prefix resolution
(`resolve_cid_prefix`), upstream port resolution (`port_upstream`), the
suspended-sandbox wake (`wake_for_preview`), the passive availability probe
(`preview`), and the 'Restart preview' rematerialize path (`ensure_preview`).

The service receives its actual collaborators directly — no runtime back-ref,
no ``rt: Any``, no multi-domain locator.

13-B3 finished the move: ``preview_upstream`` is owned here (it composes
``preview_target_port`` and ``port_upstream`` behind a ``None`` guard), and the
one-line delegators on `ConversationRuntime` are gone. Routes reach this service
by name as ``runtime.preview.<method>``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from disco.core import DEFAULT_OWNER_ID, ConversationStatus
from disco.tools import SandboxSession
from disco.tools.projects import StorageStatus
from disco.tools.sandbox.port_owner import port_owners

from .live_session_directory import LiveSessionDirectory
from .preview_projection import SealedPreviewRuntimeContract
from .preview_runtime_projection import PreviewRuntimeProjection
from .preview_sealed_restore import (
    SealedPreviewRestorer,
    normalized_sealed_runtime_contract,
    prepare_sealed_node_dependencies,
    replace_with_sealed_workspace,
    restore_succeeded,
    sealed_path,
    sealed_runtime_contract,
    sealed_workspace_cwd,
    selected_finished_app_entry,
)
from .runtime_settings import _BUILD_LIKE_SURFACES
from .workspace_commit import WorkspaceCommitUnavailable, resolve_committed_workspace

if TYPE_CHECKING:
    from disco.core.llm import ConfigStore
    from disco.core.store.sqlite import SqliteEventStore

    from .build_loop_factory import BuildLoopFactory
    from .connection_tracker import ConnectionTracker
    from .lifecycle import LifecycleManager
    from .project_runtime_service import ProjectRuntimeService
    from .runtime_settings import RuntimeSettings
    from .workspace_service import WorkspaceCoordinator

_LOG = logging.getLogger(__name__)


class PreviewService:
    def __init__(
        self,
        live_sessions: LiveSessionDirectory,
        store: SqliteEventStore,
        config_store: ConfigStore,
        settings: RuntimeSettings,
        connections: ConnectionTracker,
        projects: ProjectRuntimeService,
        lifecycle: LifecycleManager,
        loop_factory: BuildLoopFactory,
        workspace: WorkspaceCoordinator,
    ) -> None:
        self._live_sessions = live_sessions
        self._store = store
        self._settings = settings
        self._connections = connections
        self._projects = projects
        self._lifecycle = lifecycle
        self._loop_factory = loop_factory
        self._workspace = workspace
        self._runtime_projection = PreviewRuntimeProjection(
            live_sessions,
            store,
            config_store,
            settings,
            connections,
        )
        self._sealed_restorer = SealedPreviewRestorer(
            live_sessions,
            store,
            projects,
            loop_factory,
            workspace,
        )

    @asynccontextmanager
    async def capture_lease(self, conversation_id: str):
        """Keep the finished preview's sandbox owned through one HTTP capture.

        Lifecycle teardown takes the same per-conversation lock before detaching
        the executor.  The lease deliberately spans runtime resolution and the
        upstream/in-sandbox response read; a momentary workspace lock would
        still permit auto-suspend to stop the container between those phases.
        """
        async with self._connections.preview_capture_ownership.lock_for(conversation_id):
            yield

    def begin_capture(self, conversation_id: str) -> int:
        return self._connections.preview_capture_ownership.begin(conversation_id)

    def complete_capture(
        self,
        conversation_id: str,
        generation: int | None = None,
    ) -> None:
        self._connections.preview_capture_ownership.complete(conversation_id, generation)

    def live_session(self, conversation_id: str) -> SandboxSession | None:
        """Return the already-live sandbox session without creating one."""

        return self._live_sessions.live_session(conversation_id)

    def _live_browser_enabled(self) -> bool:
        return self._runtime_projection.live_browser_enabled()

    def preview_target_port(self, conversation_id: str) -> int | None:
        """Resolve the canonical preview route to its sole managed live preview.

        ``preview_start`` deliberately owns a pool of ports, so an immediate
        stop/start is allowed to move away from 8000 (for example while a
        conservative bind probe still sees the old socket as unavailable).  The
        browser capability origin and its server-side upstream must both follow the
        product-selected port; clients validate the returned port but never choose it.

        The manager tracks the last successful explicit selection and falls back to
        the newest remaining servable selection.  Once a manager exists, no healthy
        selection means unavailable rather than a hidden legacy-8000 fallback.
        """
        return self._runtime_projection.preview_target_port(conversation_id)

    async def resolve_active_preview_projection(
        self,
        conversation_id: str,
        projection: Any,
    ) -> bool:
        """Whether the exact pre-FINISHED managed preview is still live.

        This is intentionally passive apart from health/ownership probes.  It
        never composes an executor, wakes a sandbox, installs dependencies, or
        starts/restarts a command.
        """

        return await self._runtime_projection.resolve_active_preview_projection(
            conversation_id, projection
        )

    async def resolve_finished_preview_runtime(
        self,
        conversation_id: str,
        contract: SealedPreviewRuntimeContract,
    ) -> dict[str, Any] | None:
        """Resolve the exact original generation or its host-bound sealed restore."""

        return await self._runtime_projection.resolve_finished_preview_runtime(
            conversation_id, contract
        )

    @staticmethod
    def _selected_finished_app_entry(events: list[Any], marker_seq: int) -> str | None:
        return selected_finished_app_entry(events, marker_seq)

    def _sealed_runtime_contract(
        self,
        conversation_id: str,
        events: list[Any],
        committed: Any,
    ) -> SealedPreviewRuntimeContract | None:
        return sealed_runtime_contract(conversation_id, events, committed)

    @staticmethod
    def _sealed_workspace_cwd(cwd: str | None) -> str:
        """Return a jailed workspace-relative cwd for dependency restoration.

        PreviewManager launches the retained raw intent, while dependency setup
        addresses files through the sandbox file API.  Normalize the two accepted
        workspace spellings to one relative path and fail closed on anything that
        could name a different filesystem location.
        """

        return sealed_workspace_cwd(cwd)

    @staticmethod
    def _sealed_path(cwd: str, name: str) -> str:
        return sealed_path(cwd, name)

    @classmethod
    def _normalized_sealed_runtime_contract(
        cls,
        contract: SealedPreviewRuntimeContract,
    ) -> SealedPreviewRuntimeContract:
        """Canonicalize every sealed launch cwd before any runtime dispatch.

        Dependency-free Python/static/custom servers must cross the same cwd jail
        as Node runtimes.  A relative ``./`` spelling also keeps a dash-leading
        directory unambiguously path-shaped for tmux and package-manager CLIs.
        """

        return normalized_sealed_runtime_contract(contract)

    @staticmethod
    async def _prepare_sealed_node_dependencies(
        session: Any,
        contract: SealedPreviewRuntimeContract,
    ) -> str | None:
        """Restore a Node runtime from an immutable, lockfile-owned graph."""

        return await prepare_sealed_node_dependencies(session, contract)

    @staticmethod
    async def _replace_with_sealed_workspace(
        session: Any,
        files: tuple[tuple[str, bytes], ...],
        *,
        dependency_dir: str | None = None,
        contract_id: str | None = None,
    ) -> None:
        """Replace the mutable workspace with exact sealed bytes.

        When dependency installation has run, retain only its ``node_modules``
        subtree.  Lifecycle scripts may have edited source files or created other
        files, so everything else is removed and re-materialized from the verified
        immutable version before launch.
        """

        await replace_with_sealed_workspace(
            session,
            files,
            dependency_dir=dependency_dir,
            contract_id=contract_id,
        )

    @staticmethod
    def _restore_succeeded(restored: Any) -> bool:
        return restore_succeeded(restored)

    def port_upstream(self, conversation_id: str, port: int) -> str | None:
        """Resolve a curated port, gating noVNC on live-view enablement and capability."""
        return self._runtime_projection.port_upstream(conversation_id, port)

    def preview_upstream(self, conversation_id: str) -> str | None:
        """Resolve the conversation's own preview port to an upstream, or None.

        13-B3: this composition (target port, then upstream, with the None
        guard) was the one delegate in `runtime_compatibility.py` with a real
        body. It is preview policy, so it is owned here rather than assembled
        on the runtime.
        """
        port = self.preview_target_port(conversation_id)
        return self.port_upstream(conversation_id, port) if port is not None else None

    async def wake_for_preview(
        self, cid8: str, port: int, *, owner_id: str = DEFAULT_OWNER_ID
    ) -> str | None:
        """Wake a suspended sandbox if a preview request hits it.
        Restores the workspace and the built-in static preview server on 8000.
        It does NOT restart agent-started dev servers (vite/express) — requests
        for ports nothing listens on after wake will proxy to a 502.
        """
        return await self._runtime_projection.wake_for_preview(
            cid8,
            port,
            owner_id=owner_id,
            ensure_preview=self.ensure_preview,
            resolve_upstream=self.port_upstream,
        )

    async def preview(self, conversation_id: str) -> dict[str, Any]:
        """Backend-aware live preview availability. The browser iframes the agent-server's
        proxy (/conversations/{id}/preview-app/), which forwards to the active backend's
        dev server — so previews work over the tailnet via the one reachable origin, with no
        random container ports exposed. Availability is decided by whether the port is
        actually routable (expose_port / port_owners), NOT by the backend's name — a local
        rootless podman publishes real localhost URLs and previews exactly like the local
        backend; a genuinely unroutable backend still degrades honestly below."""
        return await self._runtime_projection.preview(
            conversation_id,
            port_owners_fn=port_owners,
        )

    async def _restart_manager(self, manager: Any) -> bool:
        if manager is None:
            return False
        try:
            return self._restore_succeeded(await manager.restart_canonical())
        except Exception:  # noqa: BLE001 - optional Preview remains unavailable
            return False

    async def _legacy_static_preview(
        self,
        conversation_id: str,
        store: Any,
    ) -> bool:
        if store is None or store.status() != StorageStatus.OK:
            return False
        try:
            record = store.get(conversation_id)
        except Exception:  # noqa: BLE001 - corrupt legacy snapshot stays unavailable
            return False
        if record is None or record.files_missing:
            return False
        self._loop_factory.loop_for(conversation_id)
        await self._lifecycle._maybe_rehydrate(conversation_id)
        session = self._live_sessions.live_session(conversation_id)
        if session is None:
            return False
        try:
            return await session.ensure_preview()
        except Exception:  # noqa: BLE001 - legacy compatibility stays optional
            return False

    async def _preview_without_commit(
        self,
        conversation_id: str,
        session: SandboxSession | None,
        manager: Any,
        *,
        finished: bool,
    ) -> bool:
        if finished:
            return False
        if manager is not None:
            return await self._restart_manager(manager)
        if session is None or self._settings._surface_of(conversation_id) in _BUILD_LIKE_SURFACES:
            return False
        try:
            return await session.ensure_preview()
        except Exception:  # noqa: BLE001 - legacy compatibility stays optional
            return False

    async def _restore_finished(
        self,
        conversation_id: str,
        events: list[Any],
        committed: Any,
        *,
        preserve_exact: bool,
    ) -> bool:
        contract = self._sealed_runtime_contract(conversation_id, events, committed)
        if contract is None:
            return False
        try:
            if preserve_exact and (
                await self._runtime_projection.resolve_finished_preview_runtime(
                    conversation_id,
                    contract,
                )
                is not None
            ):
                return True
            return await self._sealed_restorer.restore(
                conversation_id,
                events,
                committed,
                contract,
            )
        except Exception:  # noqa: BLE001 - optional Preview remains unavailable
            _LOG.warning(
                "sealed Preview restore failed for %s",
                conversation_id,
                exc_info=True,
            )
            return False

    async def ensure_preview(
        self,
        conversation_id: str,
        *,
        preserve_exact_finished: bool = False,
    ) -> bool:
        """Backend half of the UI 'Restart preview' button (§E7). Bounded + safe: the
        same idempotent restart as SandboxSession.ensure_preview.

        After a clean FINISH the sandbox is torn down (the G safe-leak fix in `_run`),
        which would make this button a dead affordance. Instead, re-materialize through
        the documented resume path: re-compose the loop/executor (lazy sandbox),
        rehydrate the snapshot, then start the preview — the user explicitly asked to
        see the artifact again, and that's exactly what the snapshot is for.

        Canonical capability capture sets ``preserve_exact_finished`` so an exact,
        healthy, host-verified FINISHED projection remains in place.  The explicit
        Restart Preview action keeps the default replacement behavior.
        """
        session = self._live_sessions.live_session(conversation_id)
        manager = getattr(session, "_preview_manager", None) if session is not None else None
        store = self._projects.current_project_store()

        # Preserve the legacy static artifact viewer for non-Build surfaces. It is
        # deliberately outside canonical Build Preview and cannot mint managed
        # runtime verification authority.
        if (
            session is None
            and self._settings._surface_of(conversation_id) not in _BUILD_LIKE_SURFACES
        ):
            return await self._legacy_static_preview(conversation_id, store)

        # A nonterminal managed runtime can be repaired from its in-memory accepted
        # intent. FINISHED restores require the stronger immutable contract below.
        try:
            events = await self._store.get_events(conversation_id)
            state = await self._store.get_state(conversation_id)
        except Exception:  # noqa: BLE001 - missing durable authority cannot launch code
            return False
        finished = state.execution_status is ConversationStatus.FINISHED
        if store is None or store.status() != StorageStatus.OK:
            return False if finished else await self._restart_manager(manager)
        try:
            committed = resolve_committed_workspace(events, store, conversation_id)
        except WorkspaceCommitUnavailable:
            return await self._preview_without_commit(
                conversation_id,
                session,
                manager,
                finished=finished,
            )

        if not finished:
            return await self._restart_manager(manager)
        return await self._restore_finished(
            conversation_id,
            events,
            committed,
            preserve_exact=preserve_exact_finished,
        )
