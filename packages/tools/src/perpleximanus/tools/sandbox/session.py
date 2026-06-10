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

from .base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxService,
    SandboxSpec,
    SandboxUnavailableError,
)

_LOG = logging.getLogger(__name__)


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
    ) -> None:
        self._service = service
        self._spec = spec or SandboxSpec()
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.spec = self._spec
        self._instance: SandboxInstance | None = None
        self._closed = False
        self._generation = 0  # bumped on every (re)create — telemetry + tests
        self._lock = asyncio.Lock()
        
        from .shell_sessions import ShellSessionManager
        # Process backend shares the host tmux server across conversations, so
        # session names need a per-conversation namespace; container backends get
        # an isolated tmux server each (service.name per SandboxService protocol).
        ns = f"{conversation_id[:8]}-" if service.name == "process" else ""
        self.sessions = ShellSessionManager(self._ensure, namespace=ns)

    @property
    def id(self) -> str:
        if self._instance is not None:
            return self._instance.id
        return f"session-{self.conversation_id}"

    @property
    def generation(self) -> int:
        """How many underlying instances this session has created (1 after the first
        use; >1 means it survived a death)."""
        return self._generation

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
        # the old box took the 'preview' session down with it — bring it back up
        self._spawn_auto_preview()

    def _spawn_auto_preview(self) -> None:
        """Fire-and-forget the static auto-serve (BP-02): every fresh box comes up with
        the workspace served as session 'preview'. Idempotent and polite — if anything
        already owns the port (e.g. the process backend's host, where :8000 is the
        agent-server itself), ensure_preview() backs off with False. Failures are
        logged, never raised: preview is a convenience, not a dependency of the box."""

        async def _auto() -> None:
            try:
                await self.ensure_preview()
            except Exception:  # noqa: BLE001 — best-effort; the box must not care
                _LOG.debug("auto preview start failed", exc_info=True)

        asyncio.create_task(_auto())

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

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        return await self._resilient(lambda i: i.exec_shell(cmd, timeout_s=timeout_s))

    async def read_file(self, path: str) -> bytes:
        return await self._resilient(lambda i: i.read_file(path))

    async def write_file(self, path: str, data: bytes) -> None:
        await self._resilient(lambda i: i.write_file(path, data))

    async def list_dir(self, path: str) -> list[str]:
        return await self._resilient(lambda i: i.list_dir(path))

    def display_url(self) -> str | None:
        return self._instance.display_url() if self._instance is not None else None

    def expose_port(self, port: int) -> str | None:
        return self._instance.expose_port(port) if self._instance is not None else None

    async def ensure_preview(self, port: int = 8000) -> bool:
        """Start (idempotently) the static preview as visible session 'preview'.
        Returns False without side effects if :port is already bound (someone — maybe
        the agent's own dev server — owns it; that is fine and not ours to fight)."""
        from .port_owner import port_owner

        inst = await self._ensure()
        owner = await port_owner(inst, port)
        if owner is not None and owner.pid is not None:
            return False

        res = await inst.exec_shell("pwd", timeout_s=5)
        workspace = res.stdout.strip()
        
        await self.sessions.exec(
            "preview",
            f"python3 -m http.server {port} -d {workspace}",
            exec_dir=workspace
        )
        return True

    async def destroy(self) -> None:
        """Close the session at task end: no further use, and the live box torn down."""
        self._closed = True
        inst, self._instance = self._instance, None
        if inst is not None:
            await inst.destroy()


# Structural conformance: a SandboxSession IS a SandboxInstance (drop-in for the executor).
_: type[SandboxInstance] = SandboxSession
