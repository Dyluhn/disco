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

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        self._alive()
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
        from ._container import USER_PORTS

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
