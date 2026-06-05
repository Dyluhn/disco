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
import shutil
import tempfile
import uuid
from pathlib import Path

from .base import ExecResult, SandboxError, SandboxInstance, SandboxSpec


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
        target = (self._workspace / path).resolve()
        if target != self._workspace and self._workspace not in target.parents:
            raise SandboxError(f"path escapes workspace: {path!r}")
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

    def display_url(self) -> str | None:
        return None  # the process backend has no display

    async def destroy(self) -> None:
        self._destroyed = True
        shutil.rmtree(self._workspace, ignore_errors=True)


class ProcessSandboxService:
    """[CONTRACT boundary] Creates process-backed instances (dev)."""

    name = "process"

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or tempfile.mkdtemp(prefix="pmx-sbx-")).resolve()
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
