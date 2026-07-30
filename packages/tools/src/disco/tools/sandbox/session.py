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

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ._container import PREVIEW_PORT, USER_PORTS
from .base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxService,
    SandboxSpec,
    SandboxUnavailableError,
)

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
        self._closed = False
        self._generation = 0  # bumped on every (re)create — telemetry + tests
        self._lock = asyncio.Lock()
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
        if self._closed:
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
        """Replace a dead instance with a fresh one. Idempotent under concurrent
        callers (only the first past the lock with the dead instance re-creates). A
        create() that itself fails (infra truly down) propagates as
        SandboxUnavailableError — the session can't paper over a dead host."""
        async with self._lock:
            if self._closed or self._instance is not dead:
                return  # someone else already handled it, or we were closed
            self._instance = None
            try:
                await dead.destroy()
            except Exception:  # noqa: BLE001 — it's already gone; best-effort cleanup
                pass
            self._instance = await self._service.create(
                self._spec, owner_id=self.owner_id, conversation_id=self.conversation_id
            )
            self._generation += 1
            self.sessions.reset_known_sessions()
            # C15: tear down the old ManagedKernel so its inner kernel process
            # doesn't outlive the dead sandbox (the kernel is a child of the
            # container for gateway backends, but a local subprocess for
            # process backends — we always try to shut down cleanly).
            old_kernel, self._kernel = self._kernel, None
            if old_kernel is not None:
                try:
                    await old_kernel.shutdown()
                except Exception:  # noqa: BLE001 — best-effort; the box is already gone
                    _LOG.debug("kernel shutdown on recreate failed", exc_info=True)
        # the old box took the 'preview' session down with it — bring it back up
        self._spawn_auto_preview()
        # Rehydrate the fresh (empty) workspace from the last snapshot, if the
        # owner wired a hook. Outside the lock — the hook writes files back
        # through this session, which must be able to _ensure() freely. Failures
        # are logged, never raised: the agent can always rebuild by hand, which
        # is exactly the (worse) status quo this hook exists to avoid.
        if self._on_recreate is not None:
            try:
                await self._on_recreate()
            except Exception:  # noqa: BLE001 — best-effort restore
                _LOG.warning(
                    "post-recreate rehydrate failed for %s", self.conversation_id, exc_info=True
                )
        # C5 — read-back: pull `.pmx/MEMORY.md` (the write-through mirror of
        # the in-View KnowledgeEvent channel) off the fresh box and stage
        # the facts for the agent loop to re-emit. The in-View channel is
        # authoritative in-session; the file is the durable copy. A hard
        # reset / box wipe erases the in-memory View but the file persists,
        # so this read-back is the recovery path. Best-effort: a missing
        # file (no prior remember) leaves the cache empty, and any I/O
        # failure is logged but never raised.
        try:
            await self._recover_pmx_memory()
        except Exception:  # noqa: BLE001 — recovery is a convenience, never wedge the box
            _LOG.debug("pmx memory read-back failed", exc_info=True)
        # C3: re-materialize the agent's own dev servers (vite / express /
        # uvicorn / http.server on a non-default USER_PORT, …) on the fresh
        # instance. Until this hook, only the static `python3 -m http.server`
        # preview survived a recreate — a real app the agent launched simply
        # vanished on suspend/wake. The shell-sessions manager records each
        # port-binding command in `exec()` and replays them here, best-effort,
        # skipping ports already bound (the no-duplication guarantee).
        try:
            logs = await self.sessions.rehydrate_persistent_servers()
            for line in logs:
                _LOG.info("post-recreate: %s", line)
        except Exception:  # noqa: BLE001 — rehydrate is a convenience; never wedge the box
            _LOG.warning(
                "post-recreate server rehydrate failed for %s",
                self.conversation_id,
                exc_info=True,
            )

    def _spawn_auto_preview(self) -> None:
        """Fire-and-forget the static auto-serve (BP-02): every fresh box comes up with
        the workspace served as session 'preview'. Idempotent and polite — if anything
        already owns the port (e.g. the process backend's host, where :8000 is the
        agent-server itself), ensure_preview() backs off with False. Failures are
        logged, never raised: preview is a convenience, not a dependency of the box."""

        if self._auto_preview_disabled:
            # The platform PreviewManager owns previews for this session — don't spawn
            # the legacy static auto-preview (it would race/collide on a curated port).
            return

        async def _auto() -> None:
            try:
                await self.ensure_preview()
            except Exception:  # noqa: BLE001 — best-effort; the box must not care
                _LOG.debug("auto preview start failed", exc_info=True)

        self._preview_task = asyncio.create_task(_auto())

    async def disable_auto_preview(self) -> None:
        """EPIC F (P1 #2) — stand the legacy static auto-preview DOWN so the platform
        PreviewManager is the SINGLE authority for previews on this session. Cancels a
        still-pending auto-preview task, tears down an already-running static 'preview'
        server + its tracked entry, and latches a flag so a later `_spawn_auto_preview`
        (e.g. after a sandbox recreate) does not bring it back. Idempotent and best-
        effort: preview is a convenience, so no failure here is allowed to raise."""
        self._auto_preview_disabled = True
        task, self._preview_task = self._preview_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — swallow cleanly
                pass
        try:
            await self.sessions.kill_foreground("preview")
        except Exception:  # noqa: BLE001 — nothing running / already gone is fine
            _LOG.debug("auto-preview kill on disable failed", exc_info=True)
        # Drop the static auto-preview's tracked entry (it may be registered under the
        # remapped process-safe port, so match by the well-known 'preview' name).
        for p, svc in list(self._tracked_services.items()):
            if svc.name == "preview":
                self._tracked_services.pop(p, None)

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
        """C5 — read `.disco/MEMORY.md` (legacy `.pmx/` as a fallback) from the LIVE
        instance and stage its facts in `self._recovered_memory_facts` for the agent
        loop to drain. Called from `_recreate` AFTER the user's on_recreate hook has
        run and the fresh box is up.

        A missing file (no prior `remember` calls → no facts to recover) is
        a normal, silent no-op: the cache stays None and the loop sees no
        recovery. A present-but-malformed file is logged and ignored: a
        bad mirror must not break the next step. A transient I/O error is
        also best-effort (a dead box can't recover, but the loop will
        surface the error via the next tool call anyway).
        """
        data = None
        for mem_path in (self._MEMORY_PATH, self._LEGACY_MEMORY_PATH):
            try:
                data = await self.read_file(mem_path)
                break
            except (FileNotFoundError, NotADirectoryError):
                continue  # try the legacy path, then give up
            except Exception:  # noqa: BLE001 — read flakiness on a fresh box is best-effort
                _LOG.debug("disco memory read-back failed (no facts recovered)", exc_info=True)
                self._recovered_memory_facts = None
                return
        if data is None:
            # No mirror (new or legacy) on the new box — nothing to recover. Leave
            # cache None (a fresh box / no prior remember) so the loop sees clean state.
            self._recovered_memory_facts = None
            return
        if not data:
            self._recovered_memory_facts = None
            return
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — decode error
            self._recovered_memory_facts = None
            return
        facts: list[tuple[str, str]] = []
        current_scope = ""
        for raw in text.splitlines():
            line = raw.rstrip()
            if line.startswith("## "):
                current_scope = line[3:].strip()
                continue
            # Skip the leading comment / headings — they're prose, not facts.
            if line.startswith("# "):
                continue
            # List item: `- <snippet>`. The write-through only ever produces
            # `- ` list items under `## <scope>` headings, so this parser
            # matches the writer exactly. (An empty line just advances.)
            if line.startswith("- "):
                snippet = line[2:].strip()
                if snippet:
                    facts.append((current_scope, snippet))
        self._recovered_memory_facts = facts or None

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

    # Cap on the body we'll pull back through the exec/base64 channel (Fix 2 B-E).
    # Mirrors the 25 MB per-file upload cap; base64 inflates ~33% over the wire but
    # we truncate the SOURCE at this many bytes so a runaway response can't blow up
    # the exec stdout buffer.
    _FETCH_INSIDE_CAP_BYTES = 25 * 1024 * 1024

    async def fetch_inside(
        self, port: int, path: str, *, timeout_s: int = 10
    ) -> tuple[int, bytes, str] | None:
        """Fix 2 (B-E) — a LIVENESS proxy to a server bound INSIDE the sandbox.

        On sealed/filtered network modes the backend publishes NO host port, so
        `expose_port` resolves to None even while a dev server is up. The only
        host-reachable channel is then the run-end snapshot — stale/empty mid-run.
        This bridges that gap: it `curl`s `http://127.0.0.1:{port}/{path}` from
        INSIDE the box (the one place the server IS reachable) over the existing
        exec path, base64-framing the body so binary assets (PNG/wasm/…) survive
        the text-only exec channel byte-identical.

        Returns `(status, body, content_type)` when the in-sandbox server answers,
        or `None` when it isn't up (connection refused → curl http_code 000),
        `curl` is missing, the port is not a curated USER_PORT, or the box died.
        GET only; body truncated at `_FETCH_INSIDE_CAP_BYTES`. A liveness probe
        must never raise into the preview route — every failure path yields None.
        """
        import base64
        import urllib.parse

        # Container/generic service containment stays on USER_PORTS. A process
        # sandbox may additionally fetch one of the platform's managed host
        # runtime ports; signed Preview authority, not this liveness primitive,
        # decides whether any such response is user-visible.
        from disco.core.loop.preview_target import is_managed_host_preview_port

        managed_host_port = self.shares_host_network and is_managed_host_preview_port(port)
        if port not in USER_PORTS and not managed_host_port:
            return None

        # URL-encode the path so it can't break out of the single-quoted shell arg
        # (a `'` becomes %27); preserve the URL-structural characters.
        safe_path = urllib.parse.quote(path or "", safe="/?=&%#-._~+,:@!$()*;")
        url = f"http://127.0.0.1:{int(port)}/{safe_path}"
        # curl writes the body to a temp file and prints `status\tcontent_type`
        # (no trailing newline) to stdout; we then add a newline and stream the
        # (truncated) body back base64-encoded. `;` not `&&` so we always reach the
        # base64 step — a curl failure leaves an empty/`000` header we map to None.
        cmd = (
            f"curl -s -o /tmp/.pv -w '%{{http_code}}\\t%{{content_type}}' "
            f"--max-time {int(timeout_s)} '{url}' 2>/dev/null; "
            f"printf '\\n'; "
            f"head -c {self._FETCH_INSIDE_CAP_BYTES} /tmp/.pv 2>/dev/null | base64 -w0 2>/dev/null"
        )
        try:
            res = await self.exec_shell(cmd, timeout_s=int(timeout_s) + 2)
        except Exception:  # noqa: BLE001 — a liveness probe must never raise into the route
            return None
        out = res.stdout
        nl = out.find("\n")
        if nl < 0:
            return None  # curl missing / no header line → not reachable
        header = out[:nl]
        b64 = out[nl + 1 :].strip()
        parts = header.split("\t")
        status_str = parts[0].strip()
        ctype = parts[1].strip() if len(parts) > 1 else ""
        if not status_str.isdigit():
            return None
        status = int(status_str)
        if status == 0:
            return None  # curl http_code 000 → connection refused / server not up
        try:
            body = base64.b64decode(b64) if b64 else b""
        except Exception:  # noqa: BLE001 — corrupt frame → treat as not reachable
            return None
        return (status, body, ctype or "application/octet-stream")

    async def _detect_serve_dir(self, inst: SandboxInstance, workspace: str) -> str:
        """W6 — detect the subdirectory containing index.html and serve THAT dir.

        Supports subdir apps (e.g. `macos-clone/index.html`): the preview should
        serve the subdir, not the workspace root (which shows raw files instead of
        the app). Falls back to workspace root when no index.html is found or the
        shell command fails.

        Skips `.pmx/` and `node_modules/` — those directories are internal and
        should never be the serve root."""
        try:
            res = await inst.exec_shell(
                # Find first index.html, skipping internal dirs, sort shallowest first
                f"find {shlex.quote(workspace)} -name 'index.html'"
                f" -not -path '*/.pmx/*' -not -path '*/node_modules/*'"
                f" | sort | head -1",
                timeout_s=5,
            )
            if res.exit_code == 0:
                found = res.stdout.strip()
                if found:
                    import os as _os

                    subdir = _os.path.dirname(found)
                    if subdir and subdir != workspace:
                        return subdir
        except Exception:  # noqa: BLE001 — preview is a convenience, never wedge
            pass
        return workspace

    async def ensure_preview(self, port: int = PREVIEW_PORT) -> bool:
        """Start (idempotently) the static preview as visible session 'preview'.
        Returns False without side effects if :port is already bound (someone — maybe
        the agent's own dev server — owns it; that is fine and not ours to fight).

        W6 — serves the app's index.html SUBDIRECTORY when one is detected (e.g.
        `macos-clone/`) rather than always falling back to the workspace root.
        A root-level index.html gets the original behavior.

        BP-G9 — the static preview is now ONE tracked service among potentially
        many. The entry is registered in `self._tracked_services[port]` so the
        C3 rematerialize hook re-issues it on a fresh box and a UI/runtime can
        ask "what's exposed on this conversation right now?" (see
        `tracked_services`). For multi-service builds use `ensure_service`
        directly (BP-G9 acceptance: API on 3000 + frontend on 5173)."""
        if self._auto_preview_disabled:
            # The platform PreviewManager owns previews here — back off (P1 #2).
            return False

        from disco.core.loop.preview_target import (
            process_safe_preview_port,
            reserved_control_ports,
        )

        from .port_owner import port_owner

        # Process-backend containment (Bug 7): on a SHARED-host backend (process/
        # local) the default preview port (8000) is the agent-server's own control
        # port — serving the static preview there collides with + crashes the
        # agent-server. Remap a reserved control port to a process-safe preview port
        # (8080, never 8000). Isolated container backends keep 8000 (it's the box's).
        if self._service.name in ("process", "local") and port in reserved_control_ports():
            port = process_safe_preview_port()

        inst = await self._ensure()
        owner = await port_owner(inst, port)
        if owner is not None and owner.pid is not None:
            return False

        res = await inst.exec_shell("pwd", timeout_s=5)
        workspace = res.stdout.strip()
        # W6: serve the deepest index.html directory, not always the workspace root.
        serve_dir = await self._detect_serve_dir(inst, workspace)
        # S-W5 D5: argv-serialize every component. ``serve_dir`` originates in
        # the model-authored workspace and may contain shell metacharacters;
        # interpolating it into a command made preview restart an execution sink.
        preview_argv = ["python3", "-m", "http.server", str(port), "-d", serve_dir]
        cmd = shlex.join(preview_argv)

        await self.sessions.exec("preview", cmd, exec_dir=serve_dir)
        # BP-G9: register the static preview as a tracked service so the wake
        # machinery has a single source of truth for "what to rematerialize on
        # a fresh box" — works alongside the C3 `_persistent_servers` dict that
        # `shell_exec` populates implicitly.
        self._tracked_services[port] = TrackedService(
            name="preview",
            port=port,
            command=cmd,
            exec_dir=serve_dir,
        )
        return True

    # ---- BP-G9: multi-service tracking + exposure -------------------------

    async def ensure_service(
        self,
        name: str,
        port: int,
        command: str,
        *,
        exec_dir: str | None = None,
    ) -> str | None:
        """BP-G9 — start + track a USER_PORT-binding service.

        Generalization of `ensure_preview`: the static auto-preview is one
        such service; the agent can register arbitrarily many (an API on
        3000, a Vite dev server on 5173, a worker admin UI on 8080, …).
        Each gets its own tmux session, its own URL, and its own rematerialize
        entry — so multi-service builds are first-class, not a special case
        of the single 'preview' path.

        Behavior:
          - Refuses to track a port outside `USER_PORTS` (containment: a
            non-curated port must NEVER become a tracked/exposed URL).
          - If the port is ALREADY bound on this box (the agent's own dev
            server, or another tracker's service), returns None — no fight
            (same polite-backing-off rule as `ensure_preview`).
          - Otherwise launches `command` as a tmux session named `name`
            in `exec_dir` (default: the live workspace, same as
            `ensure_preview`) and records the service in
            `self._tracked_services[port]`. Returns the exposed URL on
            success.

        The C3 rehydrate path in `_recreate` re-issues every tracked
        service on a fresh box (the recorded `command` survives the
        tmux-level reset, just like C3's `_persistent_servers`).
        """
        from .port_owner import port_owner

        if port not in USER_PORTS:
            raise SandboxError(f"port {port} is not in USER_PORTS; refusing to track as service")

        inst = await self._ensure()
        owner = await port_owner(inst, port)
        if owner is not None and owner.pid is not None:
            # Port already claimed by something on this box — record the
            # intent (so the wake machinery can see "we wanted a service
            # on this port") but DON'T fight the current owner. The
            # rematerialize skip-check will short-circuit on recreate.
            self._tracked_services[port] = TrackedService(
                name=name,
                port=port,
                command=command,
                exec_dir=exec_dir,
            )
            return None

        cwd = exec_dir
        if cwd is None:
            res = await inst.exec_shell("pwd", timeout_s=5)
            cwd = res.stdout.strip()

        await self.sessions.exec(name, command, exec_dir=cwd)
        self._tracked_services[port] = TrackedService(
            name=name,
            port=port,
            command=command,
            exec_dir=cwd,
        )
        return inst.expose_port(port)

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
        """Close the session at task end: no further use, and the live box torn down.

        Cancels any in-flight auto-preview task before tearing down the instance so
        teardown never races a half-started preview (TOCTOU fix — the task is now
        tracked as self._preview_task and cancelled here).

        P2 #4: also closes a cached platform `PreviewManager` (set on `_preview_manager`
        by the preview_* tools) — its supervisor task sleeps forever otherwise, leaking
        past the sandbox it supervised.
        """
        self._closed = True
        # The process backend's managed Jupyter kernel owns a child process and
        # multiple ZMQ channel sockets.  Tear it down while its sandbox still
        # exists; simply dropping the reference leaves the kernel and sockets
        # alive until interpreter shutdown (where ResourceWarning is too late to
        # recover them).  Container-backed kernels use the same ownership path.
        kernel, self._kernel = self._kernel, None
        if kernel is not None:
            try:
                await kernel.shutdown()
            except Exception:  # noqa: BLE001 — teardown remains best-effort
                _LOG.debug("kernel shutdown on session destroy failed", exc_info=True)
        # Close a cached PreviewManager so its supervisor task can't outlive the sandbox.
        mgr = self._preview_manager
        if mgr is not None:
            await mgr.aclose()
            self._preview_manager = None
        # Cancel the auto-preview task first so it can't race the instance teardown.
        task, self._preview_task = self._preview_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — swallow cleanly
                pass
        # Keep the instance handle retryable until destroy proves termination.
        inst = self._instance
        if inst is not None:
            await inst.destroy()
            self._instance = None


# Structural conformance: a SandboxSession IS a SandboxInstance (drop-in for the
# executor). `cast` papers over the fact that `id` is a `@property` here but a
# plain `str` attribute on the Protocol — the property *returns* a str, so the
# structural shape is honored at runtime, the type checker just can't see it.
_: type[SandboxInstance] = cast(type[SandboxInstance], SandboxSession)
