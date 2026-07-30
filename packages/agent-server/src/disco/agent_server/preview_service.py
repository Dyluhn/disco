"""Live-preview proxy + sandbox wake — extracted from `runtime.py`.

God-file decomposition (pure move, zero behavior change). The preview-facing
accessors move out of runtime.py into a `PreviewService` collaborator
constructed once in `ConversationRuntime`: cid-prefix resolution
(`resolve_cid_prefix`), upstream port resolution (`port_upstream`), the
suspended-sandbox wake (`wake_for_preview`), the passive availability probe
(`preview`), and the 'Restart preview' rematerialize path (`ensure_preview`).

The service receives its actual collaborators directly — no runtime back-ref,
no ``rt: Any``, no multi-domain locator. ``preview_upstream`` stays on the
runtime and calls the ``port_upstream`` delegator. Every moved method keeps a
one-line delegator on `ConversationRuntime` because routes call each on the
runtime (and `wake_for_preview`'s internal cross-calls route back through the
runtime).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import shlex
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from disco.core import DEFAULT_OWNER_ID, ConversationStatus, DeliverableEvent
from disco.core.loop.preview_target import is_managed_host_preview_port
from disco.tools.projects import StorageStatus
from disco.tools.sandbox._container import NOVNC_PORT, PREVIEW_PORT, USER_PORTS
from disco.tools.sandbox.port_owner import port_owners

from .preview_manager import PreviewManager, preview_requires_node_dependencies
from .preview_projection import (
    SealedPreviewRuntimeContract,
    derive_sealed_preview_runtime_contract,
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
    from .run_registry import RunResourceRegistry
    from .runtime_settings import RuntimeSettings
    from .workspace_service import WorkspaceCoordinator

_LOG = logging.getLogger(__name__)


class PreviewService:
    def __init__(
        self,
        run_resources: RunResourceRegistry,
        store: SqliteEventStore,
        config_store: ConfigStore,
        settings: RuntimeSettings,
        connections: ConnectionTracker,
        projects: ProjectRuntimeService,
        lifecycle: LifecycleManager,
        loop_factory: BuildLoopFactory,
        workspace: WorkspaceCoordinator,
    ) -> None:
        self._run_resources = run_resources
        self._store = store
        self._config_store = config_store
        self._settings = settings
        self._connections = connections
        self._projects = projects
        self._lifecycle = lifecycle
        self._loop_factory = loop_factory
        self._workspace = workspace

    def resolve_cid_prefix(self, cid8: str) -> str | None:
        """Full conversation id whose uuid part starts with cid8 — live executors only
        (a preview without a live sandbox is a 503 anyway). Ambiguous (>1) → None."""
        matches = [
            cid
            for cid in self._run_resources.conversation_ids(executors_only=True)
            if cid.removeprefix("conv_").startswith(cid8)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    async def resolve_owned_cid_prefix(self, cid8: str, owner_id: str) -> str | None:
        matches: list[str] = []
        for cid in self._run_resources.conversation_ids(executors_only=True):
            if not cid.removeprefix("conv_").startswith(cid8):
                continue
            if await self._store.conversation_owned_by(cid, owner_id):
                matches.append(cid)
        if len(matches) == 1:
            return matches[0]
        return None

    def _live_browser_enabled(self) -> bool:
        """Read the live-browser Settings flag; default-deny on any config failure."""
        try:
            return bool(self._config_store.load().live_browser.enabled)
        except Exception:  # noqa: BLE001 — config unavailable ⇒ feature OFF
            return False

    @staticmethod
    def _empty_preview_metadata(*, port: int | None = None) -> dict[str, Any]:
        return {
            "port": port,
            "status": None,
            "generation": None,
            "launch_kind": None,
            "reload_strategy": None,
            "update_error": None,
        }

    @classmethod
    def _managed_preview_metadata(cls, manager: Any) -> tuple[dict[str, Any], str]:
        """Read the canonical lifecycle snapshot without probing or launching."""

        if manager is None:
            return cls._empty_preview_metadata(), ""
        try:
            lifecycle = manager.canonical_lifecycle_session()
            data = lifecycle.to_dict() if lifecycle is not None else None
        except Exception:  # noqa: BLE001 — malformed registry fails closed
            data = None
        if not isinstance(data, dict):
            return cls._empty_preview_metadata(), ""
        port = data.get("port")
        if not isinstance(port, int) or isinstance(port, bool):
            port = None
        status = data.get("status")
        generation = data.get("generation")
        launch_kind = data.get("launch_kind")
        reload_strategy = data.get("reload_strategy")
        metadata = {
            "port": port,
            "status": status if isinstance(status, str) else None,
            "generation": generation if isinstance(generation, str) else None,
            "launch_kind": launch_kind if isinstance(launch_kind, str) else None,
            "reload_strategy": (reload_strategy if reload_strategy in {"hmr", "reload"} else None),
            "update_error": (
                data.get("update_error") if isinstance(data.get("update_error"), str) else None
            ),
        }
        detail = data.get("detail")
        return metadata, detail if isinstance(detail, str) else ""

    @staticmethod
    def _managed_unavailable_reason(status: str | None, detail: str) -> str | None:
        if status == "starting":
            return "Preparing preview: the managed runtime is starting."
        if status == "restarting":
            return "Preparing preview: the managed runtime is restarting."
        if status == "crashed":
            suffix = f" {detail}" if detail else ""
            return f"The managed preview runtime crashed.{suffix}"
        if status == "stopped":
            return "The managed preview runtime is stopped."
        if status == "unavailable":
            return detail or "The managed preview is healthy but cannot be exposed here."
        return None

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
        executor = self._run_resources.executor(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        manager = getattr(session, "_preview_manager", None) if session is not None else None
        if manager is None:
            # Owned Build sessions reserve Preview for PreviewManager from their first
            # sandbox use.  Do not guess the legacy :8000 target before a managed launch;
            # that would expose a second lifecycle authority and tell the user a runtime
            # exists when the honest state is still "Preparing preview".
            if self._settings._surface_of(
                conversation_id
            ) in _BUILD_LIKE_SURFACES or getattr(
                session, "_auto_preview_disabled", False
            ):
                return None
            return PREVIEW_PORT
        try:
            port = manager.canonical_port()
        except Exception:  # noqa: BLE001 — corrupt registry cannot select an upstream
            return None
        if port is None:
            return None
        managed_host_port = is_managed_host_preview_port(port)
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or (port not in USER_PORTS and not managed_host_port)
            or (managed_host_port and not getattr(session, "shares_host_network", False))
            or port == NOVNC_PORT
        ):
            return None
        return port

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

        executor = self._run_resources.executor(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        manager = getattr(session, "_preview_manager", None) if session is not None else None
        if manager is None:
            return False
        try:
            return await manager.resolve_active_projection(projection) is not None
        except Exception:  # noqa: BLE001 — a stale/malformed generation fails closed
            return False

    async def resolve_finished_preview_runtime(
        self,
        conversation_id: str,
        contract: SealedPreviewRuntimeContract,
    ) -> dict[str, Any] | None:
        """Resolve the exact original generation or its host-bound sealed restore."""

        executor = self._run_resources.executor(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        manager = getattr(session, "_preview_manager", None) if session is not None else None
        if manager is None:
            return None
        try:
            resolved = await manager.resolve_active_projection(contract.projection)
            if resolved is None:
                resolved = await manager.resolve_sealed_contract(contract)
            data = resolved.to_dict() if resolved is not None else None
        except Exception:  # noqa: BLE001 - stale or malformed authority fails closed
            return None
        if not isinstance(data, dict) or data.get("status") not in {"running", "unavailable"}:
            return None
        return data

    @staticmethod
    def _selected_finished_app_entry(events: list[Any], marker_seq: int) -> str | None:
        selected: str | None = None
        for event in events:
            if type(getattr(event, "seq", None)) is int and event.seq > marker_seq:
                break
            if isinstance(event, DeliverableEvent) and event.artifact_kind == "app":
                selected = event.path
        return selected

    def _sealed_runtime_contract(
        self,
        conversation_id: str,
        events: list[Any],
        committed: Any,
    ) -> SealedPreviewRuntimeContract | None:
        seal = committed.event.final_seal
        if seal is None or committed.event.seq is None:
            return None
        entry = self._selected_finished_app_entry(events, committed.event.seq)
        if entry is None:
            return None
        return derive_sealed_preview_runtime_contract(
            events,
            terminal_seq=seal.terminal_seq,
            conversation_id=conversation_id,
            version_seq=committed.event.version_seq,
            tree_digest=committed.event.tree_digest,
            app_entry=entry,
        )

    @staticmethod
    def _sealed_workspace_cwd(cwd: str | None) -> str:
        """Return a jailed workspace-relative cwd for dependency restoration.

        PreviewManager launches the retained raw intent, while dependency setup
        addresses files through the sandbox file API.  Normalize the two accepted
        workspace spellings to one relative path and fail closed on anything that
        could name a different filesystem location.
        """

        if cwd is None or cwd in {"", ".", "/workspace", "/workspace/"}:
            return ""
        if cwd != cwd.strip() or "\x00" in cwd or "\\" in cwd:
            raise RuntimeError("sealed Preview cwd must be a clean POSIX workspace path")
        if cwd.startswith("/workspace/"):
            relative = cwd.removeprefix("/workspace/")
        elif cwd.startswith("/"):
            raise RuntimeError("sealed Preview cwd must remain inside /workspace")
        else:
            relative = cwd
        parts = tuple(part for part in relative.split("/") if part not in {"", "."})
        if not parts or any(part == ".." for part in parts):
            if parts:
                raise RuntimeError("sealed Preview cwd must remain inside /workspace")
            return ""
        return "/".join(parts)

    @staticmethod
    def _sealed_path(cwd: str, name: str) -> str:
        return posixpath.join(cwd, name) if cwd else name

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

        relative_cwd = cls._sealed_workspace_cwd(contract.cwd)
        return replace(contract, cwd=f"./{relative_cwd}" if relative_cwd else None)

    @staticmethod
    async def _prepare_sealed_node_dependencies(
        session: Any,
        contract: SealedPreviewRuntimeContract,
    ) -> str | None:
        """Restore a Node runtime from an immutable, lockfile-owned graph."""

        if not preview_requires_node_dependencies(
            command=contract.command,
            framework=contract.framework,
        ):
            return None
        cwd = PreviewService._sealed_workspace_cwd(contract.cwd)
        package_path = PreviewService._sealed_path(cwd, "package.json")
        try:
            package = json.loads((await session.read_file(package_path)).decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("sealed Node Preview is missing a valid package.json") from exc
        if not isinstance(package, dict):
            raise RuntimeError("sealed Node Preview package.json must be an object")
        dependency_sections = (
            package.get("dependencies"),
            package.get("devDependencies"),
            package.get("optionalDependencies"),
        )
        needs_install = any(
            isinstance(section, dict) and bool(section) for section in dependency_sections
        )
        if not needs_install:
            return None
        cli_cwd = f"./{cwd}" if cwd else ""
        npm_prefix = f" --prefix {shlex.quote(cli_cwd)}" if cli_cwd else ""
        pnpm_dir = f" --dir {shlex.quote(cli_cwd)}" if cli_cwd else ""
        yarn_cwd = f" --cwd {shlex.quote(cli_cwd)}" if cli_cwd else ""
        lock_commands = (
            ("package-lock.json", f"npm{npm_prefix} ci --no-audit --no-fund"),
            ("npm-shrinkwrap.json", f"npm{npm_prefix} ci --no-audit --no-fund"),
            ("pnpm-lock.yaml", f"corepack pnpm{pnpm_dir} install --frozen-lockfile"),
            ("yarn.lock", f"corepack yarn{yarn_cwd} install --immutable"),
        )
        available = [
            (PreviewService._sealed_path(cwd, path), command)
            for path, command in lock_commands
            if await session.file_exists(PreviewService._sealed_path(cwd, path))
        ]
        if len(available) != 1:
            found = ", ".join(path for path, _command in available) or "none"
            raise RuntimeError(
                "sealed Node Preview requires exactly one supported immutable lockfile "
                f"(found: {found})"
            )
        lock_path, command = available[0]
        installed = await session.exec_shell(command, timeout_s=300)
        if getattr(installed, "exit_code", 1) != 0 or bool(getattr(installed, "timed_out", False)):
            detail = str(
                getattr(installed, "stderr", "")
                or getattr(installed, "stdout", "")
                or "dependency installation failed"
            ).strip()
            raise RuntimeError(
                f"sealed Node Preview dependency restore from {lock_path} failed: {detail[-1200:]}"
            )
        dependency_dir = PreviewService._sealed_path(cwd, "node_modules")
        if not await session.file_exists(dependency_dir):
            raise RuntimeError(
                "sealed Node Preview dependency restore did not produce node_modules; "
                "this package-manager layout is not yet supported for sealed replay"
            )
        return dependency_dir

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

        staged_dependency: str | None = None
        if dependency_dir is not None:
            normalized_dependency = dependency_dir.strip("/")
            if not normalized_dependency or normalized_dependency.endswith("/.."):
                raise RuntimeError("sealed Preview dependency directory is invalid")
            dependency_prefix = normalized_dependency + "/"
            if any(
                path == normalized_dependency or path.startswith(dependency_prefix)
                for path, _data in files
            ):
                raise RuntimeError("sealed workspace must not contain generated node_modules")
            if not contract_id:
                raise RuntimeError("sealed Preview dependency preservation requires authority")
            suffix = hashlib.sha256(contract_id.encode("utf-8")).hexdigest()[:24]
            staged_dependency = f".disco-preview-dependencies-{suffix}"
            staged_prefix = staged_dependency + "/"
            if any(
                path == staged_dependency or path.startswith(staged_prefix) for path, _data in files
            ):
                raise RuntimeError("sealed workspace collides with dependency staging path")
            dep_arg = shlex.quote(normalized_dependency)
            stage_arg = shlex.quote(staged_dependency)
            preserve = await session.exec_shell(
                " && ".join(
                    (
                        f"test -d {dep_arg}",
                        f"test ! -L {dep_arg}",
                        f"test ! -e {stage_arg}",
                        f"mv -- {dep_arg} {stage_arg}",
                        "find . -mindepth 1 -maxdepth 1 "
                        f"! -name {stage_arg} -exec rm -rf -- {{}} +",
                    )
                ),
                timeout_s=30,
            )
            if getattr(preserve, "exit_code", 1) != 0 or bool(
                getattr(preserve, "timed_out", False)
            ):
                detail = str(
                    getattr(preserve, "stderr", "")
                    or getattr(preserve, "stdout", "")
                    or "dependency preservation failed"
                ).strip()
                raise RuntimeError(f"sealed Preview dependency preservation failed: {detail[:500]}")
        else:
            clear = await session.exec_shell(
                "find . -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +",
                timeout_s=30,
            )
            if getattr(clear, "exit_code", 1) != 0 or bool(getattr(clear, "timed_out", False)):
                detail = str(
                    getattr(clear, "stderr", "")
                    or getattr(clear, "stdout", "")
                    or "workspace clear failed"
                ).strip()
                raise RuntimeError(f"sealed Preview workspace clear failed: {detail[:500]}")

        for path, data in files:
            await session.write_file(path, data)

        if staged_dependency is not None:
            dependency_parent = posixpath.dirname(dependency_dir or "")
            parent_arg = shlex.quote(dependency_parent or ".")
            dep_arg = shlex.quote(dependency_dir or "")
            stage_arg = shlex.quote(staged_dependency)
            restore = await session.exec_shell(
                " && ".join(
                    (
                        f"mkdir -p -- {parent_arg}",
                        f"test ! -e {dep_arg}",
                        f"mv -- {stage_arg} {dep_arg}",
                    )
                ),
                timeout_s=30,
            )
            if getattr(restore, "exit_code", 1) != 0 or bool(getattr(restore, "timed_out", False)):
                detail = str(
                    getattr(restore, "stderr", "")
                    or getattr(restore, "stdout", "")
                    or "dependency restore failed"
                ).strip()
                raise RuntimeError(f"sealed Preview dependency restore failed: {detail[:500]}")

        for path, data in files:
            if await session.read_file(path) != data:
                raise RuntimeError(f"sealed Preview read-back changed: {path}")

    @staticmethod
    def _restore_succeeded(restored: Any) -> bool:
        return getattr(getattr(restored, "status", None), "value", None) in {
            "running",
            "unavailable",
        }

    def port_upstream(self, conversation_id: str, port: int) -> str | None:
        """Resolve a curated port, gating noVNC on live-view enablement and capability."""
        executor = self._run_resources.executor(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return None
        if port not in USER_PORTS and (
            not is_managed_host_preview_port(port)
            or not getattr(session, "shares_host_network", False)
            or port != self.preview_target_port(conversation_id)
        ):
            return None
        if port == NOVNC_PORT and not (
            self._live_browser_enabled() and getattr(session, "supports_live_view", False)
        ):
            return None
        return session.expose_port(port)

    async def wake_for_preview(
        self, cid8: str, port: int, *, owner_id: str = DEFAULT_OWNER_ID
    ) -> str | None:
        """Wake a suspended sandbox if a preview request hits it.
        Restores the workspace and the built-in static preview server on 8000.
        It does NOT restart agent-started dev servers (vite/express) — requests
        for ports nothing listens on after wake will proxy to a 502.
        """
        cid = await self.resolve_owned_cid_prefix(cid8, owner_id)
        if cid is not None:
            return self.port_upstream(cid, port)

        try:
            summaries = await self._store.list_conversation_summaries(
                owner_id=owner_id, limit=500, cursor=None
            )
            matches = [
                s.conversation_id
                for s in summaries
                if s.conversation_id.removeprefix("conv_").startswith(cid8)
            ]
            if len(matches) != 1:
                return None
            cid = matches[0]
        except Exception:
            return None

        lock = self._connections.wake_lock_for(cid)
        async with lock:
            if self._run_resources.has_executor(cid):
                return self.port_upstream(cid, port)
            woke = await self.ensure_preview(cid)
            if woke:
                # W6: after ensure_preview launches the http.server tmux session,
                # the process takes a moment to bind the port.  Poll briefly so
                # expose_port()'s socket-probe finds it before we return None to
                # the route handler and the user sees a 503.
                for _ in range(10):
                    url = self.port_upstream(cid, port)
                    if url is not None:
                        return url
                    await asyncio.sleep(0.3)
                # Final attempt — best effort; a 503 is still safe.
                return self.port_upstream(cid, port)
            return None

    async def preview(self, conversation_id: str) -> dict[str, Any]:
        """Backend-aware live preview availability. The browser iframes the agent-server's
        proxy (/conversations/{id}/preview-app/), which forwards to the active backend's
        dev server — so previews work over the tailnet via the one reachable origin, with no
        random container ports exposed. Availability is decided by whether the port is
        actually routable (expose_port / port_owners), NOT by the backend's name — a local
        rootless podman publishes real localhost URLs and previews exactly like the local
        backend; a genuinely unroutable backend still degrades honestly below."""
        executor = self._run_resources.executor(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        if session is None:
            return {
                "available": False,
                "reason": "The agent hasn't started a sandbox yet.",
                "owner": None,
                "ports": [],
                **self._empty_preview_metadata(),
            }
        manager = getattr(session, "_preview_manager", None)
        metadata, managed_detail = self._managed_preview_metadata(manager)
        # Passive probe ONLY: this GET is polled by the UI, and a read path must not
        # create a sandbox (that's _ensure()'s side effect) or surface its failures
        # as a 500. No live instance → no preview, plainly stated.
        inst = getattr(session, "_instance", None)
        if inst is None:
            return {
                "available": False,
                "reason": self._managed_unavailable_reason(metadata["status"], managed_detail)
                or "The agent's sandbox isn't running yet.",
                "owner": None,
                "ports": [],
                **metadata,
            }
        target_port = self.preview_target_port(conversation_id)
        probe_ports = set(USER_PORTS)
        if target_port is not None:
            probe_ports.add(target_port)
        try:
            owners = await port_owners(inst, sorted(probe_ports))
        except Exception:  # noqa: BLE001 — a probe must never 500 the preview endpoint
            owners = {}
        owner = owners.get(target_port) if target_port is not None else None

        ns = f"pmx-{session.sessions.namespace}"

        def _owner_json(o):  # bound ports only; normalized session name
            sess = o.session
            if sess and sess.startswith(ns):
                sess = sess[len(ns) :]
            return {"pid": o.pid, "cmdline": o.cmdline, "session": sess}

        ports_payload = [
            {"port": p, "owner": _owner_json(o)}
            for p, o in sorted(owners.items())
            if o is not None and o.pid is not None
        ]

        if metadata["status"] == "unavailable":
            return {
                "available": False,
                "reason": self._managed_unavailable_reason(metadata["status"], managed_detail),
                "owner": _owner_json(owner)
                if owner is not None and owner.pid is not None
                else None,
                "ports": ports_payload,
                **metadata,
            }
        if target_port is None:
            preparing_reason = (
                "Preparing preview: waiting for the platform-managed runtime to start."
                if manager is None
                and self._settings._surface_of(conversation_id)
                in _BUILD_LIKE_SURFACES
                else None
            )
            return {
                "available": False,
                "reason": self._managed_unavailable_reason(metadata["status"], managed_detail)
                or preparing_reason
                or "No health-verified managed preview is currently available.",
                "owner": None,
                "ports": ports_payload,
                **metadata,
            }
        if owner is None or owner.pid is None:
            return {
                "available": False,
                "reason": self._managed_unavailable_reason(metadata["status"], managed_detail)
                or f"No dev server detected. Run one on port {target_port} inside the "
                "sandbox to see a live preview.",
                "owner": None,
                "ports": ports_payload,
                **(
                    metadata
                    if manager is not None
                    else self._empty_preview_metadata(port=target_port)
                ),
            }

        return {
            "available": True,
            "proxy": True,
            "owner": _owner_json(owner),
            "ports": ports_payload,
            **(metadata if manager is not None else self._empty_preview_metadata(port=target_port)),
        }

    async def ensure_preview(self, conversation_id: str) -> bool:
        """Backend half of the UI 'Restart preview' button (§E7). Bounded + safe: the
        same idempotent restart as SandboxSession.ensure_preview.

        After a clean FINISH the sandbox is torn down (the G safe-leak fix in `_run`),
        which would make this button a dead affordance. Instead, re-materialize through
        the documented resume path: re-compose the loop/executor (lazy sandbox),
        rehydrate the snapshot, then start the preview — the user explicitly asked to
        see the artifact again, and that's exactly what the snapshot is for."""
        executor = self._run_resources.executor(conversation_id)
        session = getattr(executor, "_sandbox", None) if executor is not None else None
        manager = getattr(session, "_preview_manager", None) if session is not None else None
        store = self._projects.current_project_store()

        # Preserve the legacy static artifact viewer for non-Build surfaces. It is
        # deliberately outside canonical Build Preview and cannot mint managed
        # runtime verification authority.
        if (
            executor is None
            and self._settings._surface_of(conversation_id)
            not in _BUILD_LIKE_SURFACES
        ):
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
            executor = self._run_resources.executor(conversation_id)
            session = getattr(executor, "_sandbox", None) if executor is not None else None
            if session is None:
                return False
            try:
                return await session.ensure_preview()
            except Exception:  # noqa: BLE001
                return False

        # A nonterminal managed runtime can be repaired from its in-memory accepted
        # intent. FINISHED restores require the stronger immutable contract below.
        try:
            events = await self._store.get_events(conversation_id)
            state = await self._store.get_state(conversation_id)
        except Exception:  # noqa: BLE001 - missing durable authority cannot launch code
            return False
        finished = state.execution_status is ConversationStatus.FINISHED
        if store is None or store.status() != StorageStatus.OK:
            if manager is None or finished:
                return False
            try:
                restarted = await manager.restart_canonical()
                return self._restore_succeeded(restarted)
            except Exception:  # noqa: BLE001
                return False
        try:
            committed = resolve_committed_workspace(events, store, conversation_id)
        except WorkspaceCommitUnavailable:
            if finished:
                return False
            if manager is not None:
                try:
                    restarted = await manager.restart_canonical()
                    return self._restore_succeeded(restarted)
                except Exception:  # noqa: BLE001
                    return False
            if (
                session is not None
                and self._settings._surface_of(conversation_id)
                not in _BUILD_LIKE_SURFACES
            ):
                try:
                    return await session.ensure_preview()
                except Exception:  # noqa: BLE001
                    return False
            return False

        contract = self._sealed_runtime_contract(conversation_id, events, committed)
        if contract is None:
            return False

        # Explicit user restart is the only replay boundary. Serialize it with all
        # workspace writers, consume bytes from the freshly verified immutable
        # version (never the mutable project mirror), and re-prove the seal before
        # executing the raw typed intent.
        lock = self._workspace.lock(conversation_id)
        async with lock, self._workspace.interprocess_mutation_fence(conversation_id):
            try:
                with store.open_verified_version(
                    conversation_id, committed.event.version_seq
                ) as verified:
                    files = tuple(
                        (entry.path, verified.read_bytes(entry.path)) for entry in verified.files
                    )
                if executor is None:
                    self._loop_factory.loop_for(conversation_id)
                    executor = self._run_resources.executor(conversation_id)
                session = getattr(executor, "_sandbox", None) if executor is not None else None
                if session is None:
                    return False
                manager = getattr(session, "_preview_manager", None)
                if manager is not None:
                    await manager.stop(None)
                if manager is None or bool(getattr(manager, "_closed", False)):
                    manager = PreviewManager(session)
                    session._preview_manager = manager
                await self._replace_with_sealed_workspace(session, files)
                self._lifecycle._mark_rehydrated(conversation_id)

                current_events = await self._store.get_events(conversation_id)
                current_committed = resolve_committed_workspace(
                    current_events, store, conversation_id
                )
                current_contract = self._sealed_runtime_contract(
                    conversation_id, current_events, current_committed
                )
                if current_contract is None or current_contract.contract_id != contract.contract_id:
                    return False

                launch_contract = self._normalized_sealed_runtime_contract(current_contract)
                dependency_dir = await self._prepare_sealed_node_dependencies(
                    session, launch_contract
                )
                if dependency_dir is not None:
                    await self._replace_with_sealed_workspace(
                        session,
                        files,
                        dependency_dir=dependency_dir,
                        contract_id=current_contract.contract_id,
                    )
                manager = getattr(session, "_preview_manager", None)
                if manager is None:
                    manager = PreviewManager(session)
                    session._preview_manager = manager
                restored = await manager.restore_sealed(launch_contract)
                status = getattr(getattr(restored, "status", None), "value", None)
                if not self._restore_succeeded(restored):
                    _LOG.warning(
                        "sealed Preview restore unavailable for %s: status=%s detail=%s",
                        conversation_id,
                        status,
                        getattr(restored, "detail", ""),
                    )
                    return False
                return True
            except Exception:  # noqa: BLE001 - any authority/rehydration failure stays unavailable
                _LOG.warning(
                    "sealed Preview restore failed for %s",
                    conversation_id,
                    exc_info=True,
                )
                return False
