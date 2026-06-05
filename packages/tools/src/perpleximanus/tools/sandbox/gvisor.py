"""gVisor sandbox backend — tool-sandbox-contract §5.1, driving the VM 201 host.

Fulfils `SandboxService`/`SandboxInstance` over Docker (docker-py) on the LOCAL
socket, selecting the `runsc` (gVisor) runtime. The session model: `create` starts
a keepalive container, `exec_shell` runs commands in it across calls, `destroy`
removes it (the workspace persists on the host via the bind mount).

Co-location: the local Docker socket means this backend MUST run on the Docker host
(VM 201) — the socket is local-only by deliberate design (no TCP/TLS). Running it
elsewhere is a deployment mismatch, not something this client papers over.

Transport-agnostic on purpose: the workspace is reached through `read_file` /
`write_file` / `list_dir`, never as a path the caller shares. This impl takes the
fast local-bind-mount shortcut UNDER those methods (it's co-located with the host
dir), but the interface stays clean so the stubbed SSH sibling — which has no local
path — implements the same methods over its transport.

docker-py is synchronous; every Docker call is run via `asyncio.to_thread` so it
never blocks the event loop.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
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


def _socket_to_base_url(socket: str) -> str:
    # docker-py wants the unix socket as "unix://<path>"; accept the contract's form.
    return socket


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
        host_workspace: Path,
        config: SandboxConfig,
    ) -> None:
        self.id = id
        self.owner_id = owner_id
        self.conversation_id = conversation_id
        self.spec = spec
        self._container = container
        self._host_workspace = host_workspace.resolve()
        self._cfg = config
        self._destroyed = False

    def _alive(self) -> None:
        if self._destroyed:
            raise SandboxError(f"sandbox instance {self.id} has been destroyed")

    def _resolve(self, path: str) -> Path:
        """Resolve `path` within the workspace; reject escapes (../, absolute)."""
        target = (self._host_workspace / path).resolve()
        if target != self._host_workspace and self._host_workspace not in target.parents:
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
        self._alive()
        return await asyncio.to_thread(self._resolve(path).read_bytes)

    async def write_file(self, path: str, data: bytes) -> None:
        self._alive()
        target = self._resolve(path)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        await asyncio.to_thread(_write)

    async def list_dir(self, path: str) -> list[str]:
        self._alive()
        target = self._resolve(path)
        return await asyncio.to_thread(lambda: sorted(p.name for p in target.iterdir()))

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

                client = docker.DockerClient(base_url=_socket_to_base_url(self._cfg.docker_socket))
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

    def _start_container(self, spec: SandboxSpec, instance_id: str, host_workspace: Path) -> Any:
        """All the blocking Docker work for `create`, run in a thread. Maps infra
        failures to typed errors with the real cause."""
        client = self._client()
        if not self._runsc_available(client):
            raise SandboxUnavailableError(
                f"the {self._cfg.runtime!r} runtime is not configured on the Docker host"
            )

        host_workspace.mkdir(parents=True, exist_ok=True)
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
                volumes={
                    str(host_workspace): {"bind": self._cfg.container_workspace, "mode": "rw"}
                },
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
        host_workspace = Path(self._cfg.workspace_root) / instance_id
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
