"""Process sandbox backend — tool-sandbox-contract.md §5.1 (development only).

A local subprocess with a per-instance workspace dir. Weak isolation (no
hardware/microVM boundary) — DEV ONLY; production uses the `e2b`/Firecracker
backend behind the same `SandboxService` interface (§5.1, deferred). Two
properties it DOES uphold, which the security tests rely on:
  - subprocesses run with a CLEAN minimal env — never `os.environ` — so no host
    secret or config leaks into the box (§6 rule 1, defense in depth);
  - file ops are jailed to the workspace (path-escape attempts are rejected).

[CONTRACT caveat §7 rule 3] the `process` backend does NOT truly block network
egress (that needs OS namespaces / a proxy, which `e2b`/`gvisor` provide). The
deny-by-default egress *policy* is modeled on `SandboxSpec.egress_allowed(...)`
and enforced by network-using tools; the documented guarantee here is weaker
than production.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from .base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxPermissionError,
    SandboxSpec,
    SandboxUnavailableError,
    strip_redundant_workspace_prefix,
)

# ROOT-1 (slides spiral): a genuine `/workspace`-rooted path token in a shell
# command — the leading `/workspace` AND the rest of the path up to the next token
# boundary (whitespace / quote / shell operator / end). Capturing the WHOLE token
# (not just the `/workspace` prefix) lets the rewrite RESOLVE + JAIL it via the same
# helper the file tools use, so a `..` traversal can't escape the jail.
# The lookbehind keeps it from matching a mid-path occurrence ('/foo/workspace') or a
# substring ('myworkspace'); the lookahead right after `/workspace` keeps it from
# matching a longer name ('/workspaces') — it only fires when `/workspace` is followed
# by a path separator, a token boundary, or end-of-string.
_WORKSPACE_TOKEN_RE = re.compile(
    r"(?<![\w/.])/workspace(?=/|$|[\s'\";|&<>()`])(?:/[^\s'\";|&<>()`]*)?"
)


class ProcessSandboxInstance:
    """[CONTRACT boundary] An in-subprocess instance with a jailed workspace."""

    def __init__(
        self, id: str, owner_id: str, conversation_id: str, spec: SandboxSpec, workspace: Path
    ) -> None:
        self.id = id
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.spec = spec
        self._workspace = workspace.resolve()
        self._destroyed = False

    def _alive(self) -> None:
        if self._destroyed:
            raise SandboxError(f"sandbox instance {self.id} has been destroyed")

    def _resolve(self, path: str) -> Path:
        """Resolve `path` within the workspace; reject escapes (../, absolute)."""
        path = strip_redundant_workspace_prefix(path)  # ROOT-2: workspace/foo → foo
        target = (self._workspace / path).resolve()
        if target != self._workspace and self._workspace not in target.parents:
            # W1/codex round-6: a path escaping the jail is an ACCESS denial — type it so the
            # loop's `except (PermissionError, OSError)` bookkeeping handlers catch it instead
            # of a bare SandboxError escaping and crashing the task.
            raise SandboxPermissionError(f"path escapes workspace: {path!r}")
        return target

    def _clean_env(self) -> dict[str, str]:
        # No os.environ — nothing from the host (incl. any real secret) leaks in.
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self._workspace),
            "TMPDIR": str(self._workspace),
        }

    def _rewrite_workspace_paths(self, cmd: str) -> str:
        """ROOT-1 (slides spiral): make a literal ``/workspace`` resolve in the shell.

        The build/agent prompts tell the model files live in ``/workspace``, and the
        FILE tools honor that (``strip_redundant_workspace_prefix`` maps
        ``workspace/foo`` → ``foo``, jailed to the real dir). But this backend's shell
        runs with ``cwd`` = the real per-instance ``/tmp/disco-sbx-.../sbx_.../`` dir,
        which has NO literal ``/workspace`` — so a model command like
        ``ls /workspace/deck.pptx`` exits 2 and the agent hunts around (``find /`` …).
        On the container backends ``/workspace`` genuinely exists, so this only bites
        the process (dev) backend.

        Rewrite each genuine ``/workspace``-rooted path TOKEN (word-boundary — never
        ``/workspaces`` and never a mid-substring) to its ``shlex.quote``-d absolute
        real-workspace path. Relative paths already resolve via ``cwd``, so a NEW-file
        relative path (``echo hi > out.txt``) is untouched — only a ``/workspace``
        prefix is translated.

        [SECURITY — P1] The rewrite RESOLVES + JAILS each token through ``_resolve`` —
        the SAME jail the file tools use — so the shell ``/workspace`` semantics match
        the file-tool ``/workspace`` semantics exactly (single source of truth). A
        token whose ``..`` traversal escapes the workspace (e.g.
        ``/workspace/../../etc/passwd``) makes ``_resolve`` raise
        ``SandboxPermissionError`` and the whole command is rejected (fail closed) —
        the rewrite must never itself manufacture an out-of-jail absolute path.
        """
        def _sub(m: re.Match[str]) -> str:
            # _resolve strips the redundant /workspace prefix, joins onto the real
            # workspace root, resolves, and raises SandboxPermissionError on escape.
            return shlex.quote(str(self._resolve(m.group(0))))

        return _WORKSPACE_TOKEN_RE.sub(_sub, cmd)

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self._alive()
        # Process-backend control-port containment (Bug 7) — BEST-EFFORT defense-in-
        # depth, NOT a guarantee. There is NO network namespace here, so the sandbox
        # shares the host's loopback with the agent-server; a command that binds/kills
        # a reserved control port (8000 agent-server / 8800 app-server / 5173 UI)
        # collides with + crashes it. This shell-string scan refuses the COMMON shapes
        # before running, but is trivially bypassable (raw socket.bind, renamed binary)
        # — the load-bearing Bug-7 fix is that verify no longer targets these ports
        # (see preview_target.resolve_preview_port); a netns is the robust follow-up.
        # Scoped to this backend only; an isolated container's 8000 is its own.
        from disco.core.loop.preview_target import (
            remap_reserved_preview_serve,
            reserved_control_ports,
            reserved_port_command_violation,
        )

        reserved = reserved_control_ports()
        # Bug 16 (§17 no-fluke) — a reserved-port PREVIEW SERVE (`python -m http.server
        # 8000`, including the form the agent's `shell_exec("preview", ...)` reaches us
        # as: `tmux send-keys -t disco-{cid8}-preview -l '...http.server 8000...'`) is
        # transparently REMAPPED to a process-safe port instead of refused. Without this,
        # a model that serves its deliverable on 8000/5173 is refused, establishes NO
        # preview, and the build STUCKs (`verify_no_progress`). The remapped server binds
        # a SAFE port (never 8000/8800/5173 — the Bug-7 crash vector stays CLOSED) inside
        # the conversation's preview session, so it is conversation-owned and the verify
        # resolver (`resolve_preview_port(host_shared=True)`) targets it → the build can
        # finish. Only the unambiguous http.server serve shape is remapped.
        remapped = remap_reserved_preview_serve(cmd, reserved)
        if remapped is not None:
            cmd = remapped
        # Containment (Bug 7) STILL refuses any reserved-port command that REMAINS after
        # the remap — a kill (`fuser`/`lsof`) or an arbitrary reserved bind is NEVER
        # remapped, only the serve above, so the refusal (exit 126, before the subprocess
        # launcher) holds for them. Best-effort defense-in-depth (see preview_target).
        why = reserved_port_command_violation(cmd, reserved)
        if why is not None:
            return ExecResult(exit_code=126, stdout="", stderr=why)
        cmd = self._rewrite_workspace_paths(cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(self._workspace),
            env=self._clean_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            # Report timed-out (not raised), consistent with the gVisor sibling: the
            # caller gets the flag + exit code rather than losing it to an exception.
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"command timed out after {timeout_s}s",
                timed_out=True,
            )
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )

    async def read_file(self, path: str) -> bytes:
        self._alive()
        return self._resolve(path).read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        self._alive()
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def list_dir(self, path: str) -> list[str]:
        self._alive()
        return sorted(p.name for p in self._resolve(path).iterdir())

    async def file_exists(self, path: str) -> bool:
        """[B4] Workspace-jailed existence check. The process workspace lives on
        the host FS, so this is a direct `Path.exists()` against the resolved
        target. A path that escapes the jail is False (not raised — the predicate
        named an out-of-scope path, which simply does not exist *in* the
        workspace); a plain absence is False."""
        self._alive()
        try:
            target = self._resolve(path)
        except SandboxError:
            return False
        return target.exists()

    @property
    def workspace_path(self) -> str | None:
        """W5 — expose the workspace root for C18 / C1c predicate resolution.
        Process backend: always an absolute host path (the temp dir is on the host).
        Container backends return None (they have a separate FS namespace)."""
        return str(self._workspace)

    def display_url(self) -> str | None:
        return None  # the process backend has no display

    def expose_port(self, port: int) -> str | None:
        """Dev-mode usability: host processes bind host ports directly, so hand
        back the local URL when the port is actually bound. No isolation boundary
        to defend on this backend, but stay within the curated USER set."""
        from disco.core.loop.preview_target import reserved_control_ports

        from ._container import USER_PORTS

        # Process-backend containment (Bug 7): never advertise a reserved control
        # port (8000 = agent-server, 8800 = app-server) as a user preview URL — on
        # the shared host that port is the server's own, not the build's app.
        if port in reserved_control_ports():
            return None
        if port not in USER_PORTS:
            return None
        import socket

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                pass
        except OSError:
            return None
        return f"http://127.0.0.1:{port}"

    async def destroy(self) -> None:
        self._destroyed = True
        
        # Cleanup tmux sessions on destroy (BP-01).
        # Container backends need nothing (container death kills the tmux server), 
        # but the process backend shares the host tmux server, so we must clean up explicitly.
        ns = f"{self.conversation_id[:8]}-"
        # Dual-read: kill sessions under the current `disco-{ns}` AND legacy
        # `pmx-{ns}` prefix so a rename leaves no orphaned tmux session.
        prefixes = (f"disco-{ns}", f"pmx-{ns}")

        proc = await asyncio.create_subprocess_shell(
            "tmux list-sessions -F '#{session_name}'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            for line in out.decode("utf-8").splitlines():
                if line.startswith(prefixes):
                    await asyncio.create_subprocess_shell(
                        f"tmux kill-session -t {shlex.quote(line)}"
                    )
        
        shutil.rmtree(self._workspace, ignore_errors=True)


class ProcessSandboxService:
    """[CONTRACT boundary] Creates process-backed instances (dev)."""

    name = "process"

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or tempfile.mkdtemp(prefix="disco-sbx-")).resolve()
        self._instances: dict[str, ProcessSandboxInstance] = {}

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance:
        instance_id = f"sbx_{uuid.uuid4().hex}"
        workspace = self._root / instance_id
        workspace.mkdir(parents=True, exist_ok=True)
        instance = ProcessSandboxInstance(instance_id, owner_id, conversation_id, spec, workspace)
        self._instances[instance_id] = instance
        return instance

    async def get(self, instance_id: str) -> SandboxInstance | None:
        return self._instances.get(instance_id)

    async def healthcheck(self) -> None:
        """[W-48] The process (dev) backend runs tools directly on the host — there is
        no remote endpoint or container runtime to reach, so the preflight only
        confirms the workspace ROOT is present + writable (the one local resource it
        needs). Raises ``SandboxUnavailableError`` naming the root on failure; returns
        None on success."""
        def _probe() -> None:
            try:
                self._root.mkdir(parents=True, exist_ok=True)
                probe = self._root / ".disco-healthcheck"
                probe.write_text("ok")
                probe.unlink()
            except Exception as exc:  # noqa: BLE001 — map to a typed infra error
                raise SandboxUnavailableError(
                    f"process sandbox workspace root {self._root} is not writable: {exc}"
                ) from exc

        await asyncio.to_thread(_probe)

    async def list_live_instances(self) -> list[str]:
        """Process backend has no container layer — returns empty."""
        return []

    async def destroy_by_conversation(self, conversation_id: str) -> None:
        """Process backend has no containers — no-op."""

    async def sweep_stale_workspaces(self, max_age_s: float = 7 * 86400) -> int:
        """Delete per-instance workspace dirs under _root that haven't been touched in
        max_age_s seconds (default 7 days). Returns the count removed. Safe to call at
        startup — only removes dirs that are genuinely old (not fresh instances)."""
        removed = 0
        now = time.time()
        try:
            for child in self._root.iterdir():
                if not child.is_dir():
                    continue
                try:
                    age_s = now - child.stat().st_mtime
                    if age_s >= max_age_s:
                        shutil.rmtree(child, ignore_errors=True)
                        removed += 1
                except Exception:  # noqa: BLE001 — one bad dir must not abort the sweep
                    pass
        except Exception:  # noqa: BLE001 — root may not exist
            pass
        return removed

    async def sweep_stale_roots(self, max_age_s: float = 86400) -> int:
        """Remove orphaned /tmp/disco-sbx-* (and legacy pmx-sbx-*) root dirs left by
        prior process runs.

        Scans the parent of _root (typically /tmp) for directories whose name
        starts with ``disco-sbx-`` or legacy ``pmx-sbx-`` and whose mtime is older
        than *max_age_s* seconds (default 1 day).  The live ``_root`` is always
        excluded.

        Safety properties:
        - Only touches entries whose name starts with ``disco-sbx-`` or legacy
          ``pmx-sbx-``; all other siblings are unconditionally skipped.
        - Never follows or removes symlinks (uses ``lstat`` + ``is_symlink``
          guard before ``rmtree``).
        - Best-effort: per-entry errors are swallowed so a busy/unremovable dir
          cannot abort the overall sweep.

        Returns the count of roots removed.
        """
        removed = 0
        now = time.time()
        parent = self._root.parent  # typically /tmp
        # B5 dual-prefix: clean BOTH the new disco-sbx-* roots AND the legacy
        # pmx-sbx-* roots left by pre-rename runs (str.startswith takes a tuple).
        prefixes = ("disco-sbx-", "pmx-sbx-")
        try:
            for sibling in parent.iterdir():
                if not sibling.name.startswith(prefixes):
                    continue  # prefix guard — never touch unrelated dirs
                if sibling == self._root:
                    continue  # never delete the live root
                if sibling.is_symlink():
                    continue  # never follow or remove symlinks
                if not sibling.is_dir():
                    continue
                try:
                    age_s = now - sibling.lstat().st_mtime
                    if age_s >= max_age_s:
                        shutil.rmtree(sibling, ignore_errors=True)
                        removed += 1
                except Exception:  # noqa: BLE001 — one bad dir must not abort the sweep
                    pass
        except Exception:  # noqa: BLE001 — parent may not exist / unreadable
            pass
        return removed
