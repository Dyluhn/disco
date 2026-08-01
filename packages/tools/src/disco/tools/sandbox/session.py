"""SandboxSession — a resilient, task-scoped sandbox shared across tool calls.

tool-sandbox-contract §5: a task gets ONE sandbox, created once and reused by many
tool calls (`create once → exec many → close`), not a fresh container per command.

The load-bearing behavior (carried lesson #1 from the sandbox arc): a box can die
MID-SESSION — an OOM kills the WHOLE box, not just the offending command (the VM 202
finding). When that happens, the session catches it (`SandboxUnavailableError`, the
typed death signal the backends now raise), RE-CREATES the underlying instance, and
raises a clean `SandboxError` the executor turns into an error `ToolResult` the loop
can see — it never wedges. The agent observes "the box died and was re-created", and
its next tool call runs on the fresh box.

`SandboxSession` implements the `SandboxInstance` protocol, so the `DefaultToolExecutor`
and the tools use it UNCHANGED — it's a drop-in, self-healing instance.
"""

from __future__ import annotations

# MODULE SURFACE, PRESERVED (Epic 10-B) ------------------------------------------
#
# Extraction into `session_parts/` moved the callers of these names out of this
# module; the names themselves are this module's surface and are restored here in
# the redundant-alias re-export form (`X as X`), ruff's sanctioned re-export
# marker, which needs no per-line suppression. `_LOG` is re-created here rather
# than re-exported from a part: `logging.getLogger(__name__)` must resolve to
# *this* module's logger name, and each part legitimately owns its own.
import asyncio
import logging
import shlex as shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ._container import PREVIEW_PORT
from ._container import USER_PORTS as USER_PORTS
from .base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxService,
    SandboxSpec,
    SandboxUnavailableError,
)
from .session_parts import fetch_inside as _fetch_inside_part
from .session_parts import lifecycle as _lifecycle_part
from .session_parts import memory_recovery as _memory_recovery_part
from .session_parts import preview as _preview_part
from .session_parts import recreate as _recreate_part
from .session_parts import services as _services_part
from .session_parts.lifecycle import _shutdown_session_kernel as _shutdown_session_kernel

_LOG = logging.getLogger(__name__)


@dataclass
class TrackedService:
    """BP-G9 — metadata for one USER_PORT service the session is responsible
    for. A `TrackedService` is registered when the agent (or the static
    auto-preview) starts a long-running server bound to a curated USER_PORT,
    and is the source of truth for "what's exposed on this box right now".
    The session can list every tracked service and its URL — so multi-service
    builds (API + frontend, …) are first-class, not a special case of the
    single 'preview' path.

    `name` is the tmux session name (no namespace prefix); `port` is the
    USER_PORT the service binds; `command` is the full command string
    originally issued; `exec_dir` is the cwd it was launched in. The C3
    rematerialize hook re-issues the same `command` on a fresh box, so a
    multi-service build survives suspend/wake end-to-end (BP-G9 acceptance).
    """

    name: str
    port: int
    command: str
    exec_dir: str | None


class SandboxSession:
    """[CONTRACT boundary] A task's sandbox, made resilient. Lazily creates the
    instance, reuses it across calls, and re-creates it on a mid-session death. Drop-in
    `SandboxInstance` — the executor/tools are agnostic to the self-healing."""

    def __init__(
        self,
        service: SandboxService,
        spec: SandboxSpec | None = None,
        *,
        owner_id: str = "local",
        conversation_id: str = "conv",
        on_recreate: Callable[[], Awaitable[None]] | None = None,
        kernel_idle_timeout_s: float | None = None,
        legacy_auto_preview: bool = True,
    ) -> None:
        self._service = service
        self._spec = spec or SandboxSpec()
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        # Called (best-effort) AFTER a mid-session death is replaced by a fresh
        # instance — the runtime hooks workspace rehydration here so the agent's
        # retry lands on its files, not an empty dir (bp-13 §2: the conv_f3bdc842
        # "all files were lost" production incident).
        self._on_recreate = on_recreate
        # C5 — MEMORY recovery cache. `_recreate` reads `.pmx/MEMORY.md` from
        # the fresh box and stages (scope, snippet) pairs here; the agent loop
        # drains them on its next step (via take_recovered_memory_facts) and
        # re-emits each as a KnowledgeEvent, restoring the in-View channel to
        # match the surviving on-disk mirror. The cache is consumed once
        # (take_ clears it) so the recovery fires exactly once per recreate.
        # None = no recovery attempted yet, OR a fresh box that had no
        # `.pmx/MEMORY.md` (no prior remember to recover).
        self._recovered_memory_facts: list[tuple[str, str]] | None = None
        # BP-G9 — multi-service tracking. One entry per USER_PORT this session
        # is responsible for: the static auto-preview (port 8000) is the
        # default; any agent-launched `ensure_service(name, port, command)`
        # adds another. Keyed by port so a UI/runtime can ask "is 3000
        # already claimed by something on this box?" and "what's its URL?"
        # in O(1). Survives `_recreate` — the C3 rematerialize hook re-issues
        # each tracked service on the fresh instance. `_persistent_servers`
        # (C3) is keyed by session name and is the IMPLICIT detection path
        # for `shell_exec`-launched servers; this dict is the EXPLICIT
        # registration path for `ensure_preview` / `ensure_service`. Both
        # source the wake machinery's rematerialize step.
        self._tracked_services: dict[int, TrackedService] = {}
        # C15: idle-cull threshold for the persistent CodeAct kernel. None ->
        # read DISCO_KERNEL_IDLE_TIMEOUT_S at first use; 0 disables culling.
        self._kernel_idle_timeout_s = kernel_idle_timeout_s
        self.spec = self._spec
        self._instance: SandboxInstance | None = None
        # Agent-server attaches its host-owned PreviewManager here.  Keep the
        # slot explicit (and untyped across the package boundary) so lifecycle
        # teardown does not depend on a dynamically invented attribute.
        self._preview_manager: Any | None = None
        self._closed, self._destroy_task = False, cast(asyncio.Task[Any] | None, None)
        self._generation = 0  # bumped on every (re)create — telemetry + tests
        self._lock, self._destroy_lock = asyncio.Lock(), asyncio.Lock()
        self._preview_task: asyncio.Task[None] | None = None  # tracked so destroy() can cancel
        # EPIC F (P1 #2): owned Build runtimes set ``legacy_auto_preview=False`` so
        # PreviewManager is the only preview lifecycle authority from the first sandbox
        # use.  The default remains available to lower-level/legacy SandboxSession
        # callers that explicitly depend on the old convenience server.  Once disabled,
        # a recreation must never resurrect the fire-and-forget static server.
        self._auto_preview_disabled = not legacy_auto_preview

        from .shell_sessions import ShellSessionManager

        # Process backend shares the host tmux server across conversations, so
        # session names need a per-conversation namespace; container backends get
        # an isolated tmux server each (service.name per SandboxService protocol).
        ns = f"{conversation_id[:8]}-" if service.name == "process" else ""
        self.sessions = ShellSessionManager(self._ensure, namespace=ns)
        self._kernel: Any | None = None

    @property
    def id(self) -> str:
        if self._instance is not None:
            return self._instance.id
        return f"session-{self.conversation_id}"

    @property
    def backend_name(self) -> str:
        """[W-48(c)] The name of the backend service this session composes on
        ('process'|'gvisor'|'local'|'podman'). Lets the runtime reconcile cached
        sessions against a changed Settings backend — a session whose backend no
        longer matches the configured one is destroyed so the next kick composes a
        fresh sandbox on the new backend (never leaking the old backend's box)."""
        return self._service.name

    @property
    def supports_live_view(self) -> bool:
        """True only when THIS session's backend can actually run + stream the noVNC
        live-view stack (Xvfb/x11vnc/websockify). The honest streamability truth read by
        the agent-server's /browser/live-ready & /browser/live-url routes so the UI never
        auto-starts (or claims) a stack a backend can't run — e.g. the process dev backend
        has no Xvfb on the host, which is exactly the source of the old
        "live_start_failed: Failed to start live view stack" error. Sourced from the ONE
        LIVE_VIEW_BACKENDS set (see _container.py) so it can't drift from the Settings
        enable-guard."""
        from ._container import LIVE_VIEW_BACKENDS

        return self._service.name in LIVE_VIEW_BACKENDS

    @property
    def generation(self) -> int:
        """How many underlying instances this session has created (1 after the first
        use; >1 means it survived a death)."""
        return self._generation

    @property
    async def kernel(self) -> Any:
        """The persistent IPython kernel for this session. Lazily created on first
        use; transport chosen by backend. Wrapped in a `ManagedKernel` (C15) that
        culls the inner kernel after `kernel_idle_timeout_s` of inactivity and
        re-spawns on the next exec — so a quiet conversation doesn't hold a
        dead kernel's RAM forever."""
        if self._kernel is None:
            inst = await self._ensure()
            from .kernel import ManagedKernel, _default_idle_timeout_s

            timeout = (
                self._kernel_idle_timeout_s
                if self._kernel_idle_timeout_s is not None
                else _default_idle_timeout_s()
            )
            if self._service.name == "process":
                from .kernel import ProcessKernel

                res = await inst.exec_shell("pwd", timeout_s=5)
                ws = res.stdout.strip()
                self._kernel = ManagedKernel(
                    lambda: ProcessKernel(ws),
                    idle_timeout_s=timeout,
                )
            else:
                from .kernel import GatewayKernel

                self._kernel = ManagedKernel(
                    lambda: GatewayKernel(inst, self.sessions),
                    idle_timeout_s=timeout,
                )
        return self._kernel

    async def _ensure(self) -> SandboxInstance:
        if self._closed and asyncio.current_task() is not self._destroy_task:
            raise SandboxError("sandbox session is closed")
        created = False
        async with self._lock:
            if self._instance is None:
                self._instance = await self._service.create(
                    self._spec, owner_id=self.owner_id, conversation_id=self.conversation_id
                )
                self._generation += 1
                created = True
        if created:
            self._spawn_auto_preview()
        return self._instance

    async def _recreate(self, dead: SandboxInstance) -> None:
        """Replace a dead instance with a fresh one and run every best-effort
        recovery hook (on_recreate rehydrate, C5 memory read-back, C3 server
        rematerialize); see `session_parts.recreate` for the full contract."""
        await _recreate_part.recreate(self, dead)

    def _spawn_auto_preview(self) -> None:
        """Fire-and-forget the static auto-serve (BP-02); see
        `session_parts.preview` for the full contract."""
        _preview_part.spawn_auto_preview(self)

    async def disable_auto_preview(self) -> None:
        """EPIC F (P1 #2) — stand the legacy static auto-preview DOWN; see
        `session_parts.preview` for the full contract."""
        await _preview_part.disable_auto_preview(self)

    async def _resilient(self, op):
        """Run one instance op; on a typed mid-session death, re-create and raise a
        clean SandboxError (→ the executor's `sandbox_error` ToolResult) so the loop
        sees it and the NEXT call lands on the fresh box. Never wedges."""
        inst = await self._ensure()
        try:
            return await op(inst)
        except SandboxUnavailableError as exc:
            await self._recreate(inst)
            raise SandboxError(
                f"sandbox died mid-session and was re-created (generation {self._generation}); "
                f"retry the action"
            ) from exc

    # ---- C5: MEMORY write-through read-back ---------------------------------

    # Path of the on-disk MEMORY mirror. Must match engine.py's _MEMORY_PATH
    # exactly — these two are the only writers/readers of the file and the
    # read-back's parse (## scope heading + list items) must match the
    # write-through's format. `_LEGACY_MEMORY_PATH` (the pre-rename `.pmx/` path)
    # is read as a fallback so a resumed pre-upgrade workspace isn't lost.
    _MEMORY_PATH = ".disco/MEMORY.md"
    _LEGACY_MEMORY_PATH = ".pmx/MEMORY.md"

    async def _recover_pmx_memory(self) -> None:
        """C5 — read `.disco/MEMORY.md` (legacy `.pmx/` as a fallback) and stage its
        facts in `self._recovered_memory_facts`; see `session_parts.memory_recovery`
        for the full contract."""
        await _memory_recovery_part.recover_pmx_memory(self)

    def take_recovered_memory_facts(self) -> list[tuple[str, str]]:
        """C5 — pop the staged recovery facts (set by `_recover_pmx_memory`).
        The agent loop calls this on each step to drain the cache; it
        returns an empty list and clears the cache if nothing is staged.
        Consuming once-per-recreate is the contract: a second call on
        the same session returns []. The list elements are `(scope, snippet)`
        pairs ready to be re-emitted as KnowledgeEvents.
        """
        facts = self._recovered_memory_facts or []
        self._recovered_memory_facts = None
        return facts

    def peek_recovered_memory_facts(self) -> list[tuple[str, str]]:
        """C5 — read-only variant of `take_recovered_memory_facts` for tests
        and diagnostics. Does NOT clear the cache — use `take_` to consume.
        """
        return list(self._recovered_memory_facts or [])

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        return await self._resilient(lambda i: i.exec_shell(cmd, timeout_s=timeout_s))

    async def read_file(self, path: str) -> bytes:
        return await self._resilient(lambda i: i.read_file(path))

    async def write_file(self, path: str, data: bytes) -> None:
        await self._resilient(lambda i: i.write_file(path, data))

    async def delete_file(self, path: str) -> None:
        await self._resilient(lambda i: i.delete_file(path))

    async def atomic_write(self, path: str, data: bytes) -> None:
        """Use the backend's atomic primitive when available, otherwise its ordinary write.

        ProcessSandbox and the production container backends implement ``atomic_write``.
        Compatibility backends without it retain a best-effort single write and make no
        filesystem atomicity claim; in-memory prevalidation prevents logic failures before
        dispatch but cannot make a backend write transactional.
        """

        async def _aw(i: SandboxInstance) -> None:
            fn = getattr(i, "atomic_write", None)
            if fn is not None:
                await fn(path, data)
            else:
                await i.write_file(path, data)

        await self._resilient(_aw)

    async def resolve_relpath(self, path: str) -> str:
        """CD-TOOLS-4 — delegate the REAL (symlink-followed) workspace-relative resolution so the
        governed-artifact guard is symlink-proof through this session wrapper too (not just on a
        raw instance), on every backend."""
        return await self._resilient(lambda i: i.resolve_relpath(path))

    async def list_dir(self, path: str) -> list[str]:
        return await self._resilient(lambda i: i.list_dir(path))

    async def list_dir_bounded(self, path: str, limit: int) -> tuple[list[tuple[str, str]], bool]:
        return await self._resilient(lambda i: i.list_dir_bounded(path, limit))

    async def export_workspace_archive(
        self, destination: Path, *, max_depth: int, max_file_bytes: int
    ) -> tuple[list[str], list[str]] | None:
        """Use a backend bulk-export capability when one is available.

        ``None`` preserves the transport-agnostic list/read walker for process,
        gVisor, and injected test backends.  The resilient wrapper keeps Podman's
        one-exec fast path under the same recreate/error contract as ordinary I/O.
        """

        async def _export(
            instance: SandboxInstance,
        ) -> tuple[list[str], list[str]] | None:
            exporter = getattr(instance, "export_workspace_archive", None)
            if not callable(exporter):
                return None
            export_fn = cast(Callable[..., Awaitable[tuple[list[str], list[str]]]], exporter)
            return await export_fn(destination, max_depth=max_depth, max_file_bytes=max_file_bytes)

        return await self._resilient(_export)

    async def file_exists(self, path: str) -> bool:
        """[B4] Delegate the existence check to the live instance (which resolves
        in its OWN namespace — host FS for process, inside-the-box for container).
        Goes through `_resilient`, so a mid-session box death re-creates and
        raises a clean SandboxError the C18 advisory treats as unverifiable."""
        return await self._resilient(lambda i: i.file_exists(path))

    @property
    def workspace_path(self) -> str | None:
        """W5 — expose the workspace root for C18 / C1c predicate resolution.
        Delegates to the inner SandboxInstance; None when no instance exists yet
        or when the backend doesn't expose a host-side path (container backends)."""
        if self._instance is not None:
            return getattr(self._instance, "workspace_path", None)
        return None

    @property
    def shares_host_network(self) -> bool:
        """Preserve the backend's explicit network-namespace capability.

        Tools receive this resilient wrapper in production, not the raw instance.
        Capability consumers must therefore see the same answer before and after
        lazy creation. Delegate an explicit live-instance flag when present; the
        process service is the sole host-shared built-in backend.
        """
        if self._instance is not None:
            flag = getattr(self._instance, "shares_host_network", None)
            if isinstance(flag, bool):
                return flag
        return self._service.name == "process"

    def display_url(self) -> str | None:
        return self._instance.display_url() if self._instance is not None else None

    def expose_port(self, port: int) -> str | None:
        return self._instance.expose_port(port) if self._instance is not None else None

    async def fetch_inside(
        self, port: int, path: str, *, timeout_s: int = 10
    ) -> tuple[int, bytes, str] | None:
        """Fix 2 (B-E) — a LIVENESS proxy to a server bound INSIDE the sandbox;
        see `session_parts.fetch_inside` for the full contract."""
        return await _fetch_inside_part.fetch_inside(self, port, path, timeout_s=timeout_s)

    async def _detect_serve_dir(self, inst: SandboxInstance, workspace: str) -> str:
        """W6 — detect the subdirectory containing index.html and serve THAT dir;
        see `session_parts.preview` for the full contract."""
        return await _preview_part.detect_serve_dir(inst, workspace)

    async def ensure_preview(self, port: int = PREVIEW_PORT) -> bool:
        """Start (idempotently) the static preview as visible session 'preview'
        (BP-02/W6/BP-G9); see `session_parts.preview` for the full contract."""
        return await _preview_part.ensure_preview(self, port)

    # ---- BP-G9: multi-service tracking + exposure -------------------------

    async def ensure_service(
        self,
        name: str,
        port: int,
        command: str,
        *,
        exec_dir: str | None = None,
    ) -> str | None:
        """BP-G9 — start + track a USER_PORT-binding service; see
        `session_parts.services` for the full contract."""
        return await _services_part.ensure_service(self, name, port, command, exec_dir=exec_dir)

    def tracked_services(self) -> list[TrackedService]:
        """BP-G9 — snapshot of every USER_PORT service this session is
        currently tracking, in registration order. Read-only view for
        the UI/runtime to see "what's exposed on this box right now".

        Returned list is a fresh copy — callers may mutate it without
        affecting session state. Each `TrackedService` has a `.port`,
        `.name`, `.command`, `.exec_dir` attribute. To get the URL of
        a tracked service, call `session.expose_port(svc.port)`.
        """
        return list(self._tracked_services.values())

    def tracked_ports(self) -> list[int]:
        """BP-G9 — convenience accessor: the sorted list of USER_PORTS
        with a tracked service. O(1) membership check counterpart to
        `tracked_services()`. Mirrors the `expose_port` gate's view of
        the world (only curated USER_PORTS ever appear here).
        """
        return sorted(self._tracked_services.keys())

    async def destroy(self) -> None:
        """Close the session at task end: no further use, and the live box torn
        down; see `session_parts.lifecycle` for the full contract."""
        await _lifecycle_part.destroy(self)


# Structural conformance: a SandboxSession IS a SandboxInstance (drop-in for the
# executor). `cast` papers over the fact that `id` is a `@property` here but a
# plain `str` attribute on the Protocol — the property *returns* a str, so the
# structural shape is honored at runtime, the type checker just can't see it.
_: type[SandboxInstance] = cast(type[SandboxInstance], SandboxSession)
