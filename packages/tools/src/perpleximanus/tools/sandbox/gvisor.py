"""gVisor sandbox backend — tool-sandbox-contract §5.1, driving the VM 201 host.

Fulfils `SandboxService`/`SandboxInstance` over Docker (docker-py) on the LOCAL
socket, selecting the `runsc` (gVisor) runtime. The session model: `create` starts
a keepalive container, `exec_shell` runs commands in it across calls, `destroy`
removes it (the workspace persists on the host via the bind mount).

Transport: the Docker endpoint is config-driven (`docker_socket`). It works over the
LOCAL socket when co-located on VM 201, AND over Docker-over-SSH (`ssh://user@host`,
using the system ssh client — e.g. keyless Tailscale SSH) when the agent-server runs
elsewhere on the tailnet. Both are real positions, not a papered-over mismatch.

Transport-agnostic file ops: the workspace is reached through `read_file` /
`write_file` / `list_dir`, implemented via `docker exec`/`cp` against the container
— NOT a local host path. So they work whether the backend is co-located (local
socket) or remote (Docker-over-SSH), and the stubbed SSH sibling implements the same
methods. The host bind-mount still persists the workspace on the daemon host across
the container's life; the backend just never touches that path directly.

docker-py is synchronous; every Docker call is run via `asyncio.to_thread` so it
never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import io
import posixpath
import tarfile
import uuid
from typing import Any

from ..anatomy import Capability
from .base import (
    ExecResult,
    SandboxError,
    SandboxInstance,
    SandboxSpec,
    SandboxUnavailableError,
)
from .config import SandboxConfig, default_sandbox_config

# Exit codes the `timeout` coreutil reports when it fires (SIGTERM / then SIGKILL).
_TIMEOUT_EXIT_CODES = frozenset({124, 137})


def _sealed(spec: SandboxSpec) -> bool:
    """Network is SEALED unless the capability set grants it. Per §7 the grant is
    a non-empty egress allowlist (deny-by-default); NETWORK in `permitted` also
    counts as a raw-egress grant. Default => sealed."""
    return not spec.egress_allow and Capability.NETWORK not in spec.permitted


class GvisorSandboxInstance:
    """A running gVisor container. Tools execute against it; the workspace is the
    host bind-mount, reached only through the file methods (never exposed as a path)."""

    def __init__(
        self,
        *,
        id: str,
        owner_id: str,
        conversation_id: str,
        spec: SandboxSpec,
        container: Any,
        host_workspace: str,
        config: SandboxConfig,
    ) -> None:
        self.id = id
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.spec = spec
        self._container = container
        self._host_workspace = host_workspace  # a path on the DAEMON host (info only)
        self._cfg = config
        self._ws = config.container_workspace  # the in-container workspace root
        self._destroyed = False

    def _alive(self) -> None:
        if self._destroyed:
            raise SandboxError(f"sandbox instance {self.id} has been destroyed")

    def _container_path(self, path: str) -> str:
        """Resolve `path` to an absolute path INSIDE the container's workspace,
        rejecting escapes (../, absolute). File ops go through the container (docker
        exec / cp), never a host path — so this works over any Docker transport
        (local socket or Docker-over-SSH), not just when co-located."""
        target = posixpath.normpath(posixpath.join(self._ws, path))
        if target != self._ws and not target.startswith(self._ws + "/"):
            raise SandboxError(f"path escapes workspace: {path!r}")
        return target

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        """Run `cmd` in the live container, capturing stdout/stderr/exit code. The
        timeout is enforced INSIDE the container (the `timeout` coreutil kills the
        process), with an outer backstop in case the exec call itself hangs. A
        killed command is reported with `timed_out=True`, not raised."""
        self._alive()
        # `timeout` sends SIGTERM at timeout_s, SIGKILL 5s later if it ignores it.
        wrapped = ["timeout", "-k", "5", str(timeout_s), "sh", "-c", cmd]

        def _run() -> Any:
            return self._container.exec_run(
                wrapped, demux=True, workdir=self._cfg.container_workspace
            )

        try:
            res = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_s + 15)
        except TimeoutError:
            # The exec call didn't return — the in-container timeout should have
            # fired; surface it as a timeout rather than hanging.
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"command exceeded its {timeout_s}s timeout",
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 — surface the real backend cause
            raise SandboxError(f"exec failed in {self.id}: {exc}") from exc

        exit_code = res.exit_code if res.exit_code is not None else -1
        out, err = res.output if res.output is not None else (None, None)
        return ExecResult(
            exit_code=exit_code,
            stdout=(out or b"").decode("utf-8", errors="replace"),
            stderr=(err or b"").decode("utf-8", errors="replace"),
            timed_out=exit_code in _TIMEOUT_EXIT_CODES,
        )

    async def read_file(self, path: str) -> bytes:
        """Read a workspace file via `docker exec cat` — binary-safe, transport-
        agnostic. Missing/unreadable file → SandboxError."""
        self._alive()
        target = self._container_path(path)

        def _read() -> bytes:
            res = self._container.exec_run(["cat", "--", target], demux=True)
            if res.exit_code != 0:
                err = (res.output[1] if res.output else b"") or b""
                raise SandboxError(f"read_file {path!r}: {err.decode('utf-8', 'replace').strip()}")
            return (res.output[0] if res.output else b"") or b""

        return await asyncio.to_thread(_read)

    async def write_file(self, path: str, data: bytes) -> None:
        """Write a workspace file via `docker cp` (put_archive) — binary-safe. The
        parent dir is created in the container first."""
        self._alive()
        target = self._container_path(path)
        parent = posixpath.dirname(target) or self._ws
        name = posixpath.basename(target)

        def _write() -> None:
            self._container.exec_run(["mkdir", "-p", "--", parent])
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            buf.seek(0)
            if not self._container.put_archive(parent, buf.getvalue()):
                raise SandboxError(f"write_file {path!r} failed")

        await asyncio.to_thread(_write)

    async def list_dir(self, path: str) -> list[str]:
        """List a workspace dir via `docker exec ls`."""
        self._alive()
        target = self._container_path(path)

        def _list() -> list[str]:
            res = self._container.exec_run(["ls", "-1A", "--", target], demux=True)
            if res.exit_code != 0:
                err = (res.output[1] if res.output else b"") or b""
                raise SandboxError(f"list_dir {path!r}: {err.decode('utf-8', 'replace').strip()}")
            out = (res.output[0] if res.output else b"") or b""
            return sorted(n for n in out.decode("utf-8", "replace").splitlines() if n)

        return await asyncio.to_thread(_list)

    def display_url(self) -> str | None:
        return None  # no noVNC display on this backend (yet)

    async def destroy(self) -> None:
        """Stop + remove the container. The workspace dir persists on the host
        (bind mount) — only the container is ephemeral."""
        if self._destroyed:
            return
        self._destroyed = True

        def _teardown() -> None:
            try:
                self._container.stop(timeout=self._cfg.stop_timeout_s)
            except Exception:  # noqa: BLE001 — best-effort stop; force-remove next
                pass
            try:
                self._container.remove(force=True)
            except Exception:  # noqa: BLE001 — already gone is fine
                pass

        await asyncio.to_thread(_teardown)


class GvisorSandboxService:
    """[CONTRACT boundary] Creates gVisor-backed instances on the VM 201 host."""

    name = "gvisor"

    def __init__(self, config: SandboxConfig | None = None, *, client: Any | None = None) -> None:
        self._cfg = config or default_sandbox_config()
        self._injected_client = client  # test seam: a fake docker client
        self._client_cache: Any | None = None
        self._instances: dict[str, GvisorSandboxInstance] = {}

    def _client(self) -> Any:
        """The Docker client (lazy, cached). A connection failure is a typed
        infra error carrying the real cause — Docker isn't reachable."""
        if self._injected_client is not None:
            return self._injected_client
        if self._client_cache is None:
            try:
                import docker

                base_url = self._cfg.docker_socket
                kwargs: dict[str, Any] = {"base_url": base_url}
                if base_url.startswith("ssh://"):
                    # Docker-over-SSH: use the SYSTEM ssh client so the host's auth
                    # (e.g. keyless Tailscale SSH) applies, not docker-py's paramiko.
                    kwargs["use_ssh_client"] = True
                client = docker.DockerClient(**kwargs)
                client.ping()
            except Exception as exc:  # noqa: BLE001 — map to a typed infra error
                raise SandboxUnavailableError(
                    f"Docker unreachable at {self._cfg.docker_socket}: {exc}"
                ) from exc
            self._client_cache = client
        return self._client_cache

    def _runsc_available(self, client: Any) -> bool:
        try:
            runtimes = client.info().get("Runtimes", {}) or {}
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailableError(f"could not query Docker runtimes: {exc}") from exc
        return self._cfg.runtime in runtimes

    def _start_container(self, spec: SandboxSpec, instance_id: str, host_workspace: str) -> Any:
        """All the blocking Docker work for `create`, run in a thread. Maps infra
        failures to typed errors with the real cause. `host_workspace` is a path on
        the DAEMON host (not necessarily local)."""
        client = self._client()
        if not self._runsc_available(client):
            raise SandboxUnavailableError(
                f"the {self._cfg.runtime!r} runtime is not configured on the Docker host"
            )

        # NOTE: do NOT mkdir the workspace locally — the bind-mount source is a path
        # on the Docker DAEMON host (VM 201), which Docker creates on demand. Making
        # it here would (wrongly) create it on whatever host runs the backend.
        sealed = _sealed(spec)
        mem_mb = spec.memory_mb or self._cfg.default_memory_mb
        cpu = spec.cpu or self._cfg.default_cpu

        from docker.errors import ImageNotFound

        try:
            return client.containers.run(
                image=self._cfg.image,
                command=["sleep", "infinity"],  # keepalive: stays up for exec_shell
                runtime=self._cfg.runtime,  # gVisor
                # Sealed by default: no network unless the capability set granted it.
                network_mode="none" if sealed else "bridge",
                mem_limit=f"{mem_mb}m",
                nano_cpus=int(cpu * 1_000_000_000),
                volumes={host_workspace: {"bind": self._cfg.container_workspace, "mode": "rw"}},
                # NO host env: nothing from the agent-server's environment leaks in.
                # Only capability-granted values would be added here (none by default).
                environment={},
                working_dir=self._cfg.container_workspace,
                detach=True,
                name=f"pmx-sbx-{instance_id}",
            )
        except ImageNotFound as exc:
            raise SandboxUnavailableError(
                f"sandbox image {self._cfg.image!r} not found on the host"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
            raise SandboxUnavailableError(f"container failed to start: {exc}") from exc

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance:
        instance_id = f"sbx_{uuid.uuid4().hex}"
        # A path on the DAEMON host (VM 201) — kept as a posix string, not a local Path.
        host_workspace = posixpath.join(self._cfg.workspace_root, instance_id)
        container = await asyncio.to_thread(
            self._start_container, spec, instance_id, host_workspace
        )
        instance = GvisorSandboxInstance(
            id=instance_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
            spec=spec,
            container=container,
            host_workspace=host_workspace,
            config=self._cfg,
        )
        self._instances[instance_id] = instance
        return instance

    async def get(self, instance_id: str) -> SandboxInstance | None:
        return self._instances.get(instance_id)
