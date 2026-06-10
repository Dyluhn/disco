"""Podman sandbox backend — tool-sandbox-contract §5.1, the remote/SSH-class path.

Fulfils `SandboxService`/`SandboxInstance` over Podman's NATIVE REMOTE, reached
keyless over Tailscale SSH. Same interface as the gVisor backend — second transport
(SSH) + runtime (crun). Shares the lifecycle/jail with `ContainerInstance`.

Three load-bearing Podman constraints:

  1. NATIVE REMOTE over the socket, NEVER a bare-SSH `podman run`. Driving Podman
     THROUGH THE SOCKET means limits are enforced by the `user@` systemd manager; a
     `podman run` over a non-login SSH session silently falls back to cgroupfs and
     limits DON'T apply (verified live: a socket-run container is OOM-bounded). Every
     op here goes through the socket.

  2. IMAGE BY LOAD, NEVER PULL — present-or-fail-loud (SandboxUnavailableError), and
     `containers.create` (not `run`), so nothing ever pulls from a registry.

  3. Workspace = a per-run NAMED VOLUME (Podman doesn't auto-create bind sources and
     rootless can't write root-owned host paths). Auto-created, persists.

Transport split (a real podman-py-vs-docker-py difference, verified live): podman-py
drives create/lifecycle/volumes/images/`put_archive` cleanly, but its `exec_run`
does NOT work over the remote API — it returns no output, doesn't block, and gives a
bogus exit code. So command execution + read/list go through the podman CLI's native
remote (`podman --url ssh://…//socket exec`), which captures stdout/stderr, waits,
and reports the real exit code — still socket-mediated (limits apply), not bare SSH.
The host running this backend needs the `podman` CLI + system `ssh` on PATH.
"""

from __future__ import annotations

import asyncio
import io
import posixpath
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable
from typing import Any

from ._container import TIMEOUT_EXIT_CODES, ContainerInstance, egress_mode
from .base import ExecResult, SandboxError, SandboxInstance, SandboxSpec, SandboxUnavailableError
from .config import SandboxConfig, default_podman_config

# `podman exec` stderr markers that mean the CONTAINER is gone (not the inner command
# failing) — used to type a mid-session death as SandboxUnavailableError.
_PODMAN_DEAD_MARKERS = ("no such container", "no container with", "is not running", "improper")

_CPU_PERIOD = 100_000  # cgroup CPU period (100ms); quota/period = cpus

# A CLI runner: argv -> (exit_code, stdout, stderr). Injectable for hermetic tests.
CliRunner = Callable[[list[str], float], "tuple[int, bytes, bytes]"]


def _default_cli_runner(argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)  # noqa: S603
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or b"", (exc.stderr or b"") + b"\n[backend: exec timed out]"
    except FileNotFoundError as exc:
        raise SandboxError(f"the `podman` CLI is required on PATH for exec: {exc}") from exc


class PodmanSandboxInstance(ContainerInstance):
    """A running Podman (crun, rootless) container. Lifecycle/jail are shared; exec
    and read/list use the podman CLI native remote (podman-py's exec is unusable
    over the remote API), while write uses podman-py `put_archive`."""

    def __init__(
        self,
        *,
        id: str,
        owner_id: str,
        conversation_id: str,
        spec: SandboxSpec,
        container: Any,
        container_workspace: str,
        stop_timeout_s: int,
        cli_url: str,
        container_name: str,
        cli_runner: CliRunner,
        workspace_uid: int = 1000,
    ) -> None:
        super().__init__(
            id=id,
            owner_id=owner_id,
            conversation_id=conversation_id,
            spec=spec,
            container=container,
            container_workspace=container_workspace,
            stop_timeout_s=stop_timeout_s,
            workspace_uid=workspace_uid,
        )
        self._cli_url = cli_url
        self._name = container_name
        self._runner = cli_runner

    def expose_port(self, port: int) -> str | None:
        """[STUB in this environment] Preview port exposure for the Podman backend is not
        wired here (VM 202 destroyed). The Podman backend code is real + verified, but the
        preview tunnel for it is completed at the Meta deployment. Returns None (the
        agent-server surfaces the honest labeled reason) — never a fake URL."""
        return None

    def _exec(self, argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
        return self._runner(["podman", "--url", self._cli_url, "exec", self._name, *argv], timeout)

    def _raise_if_dead(self, rc: int, err: bytes) -> None:
        """`podman exec` against a gone/exited container fails at the container level
        (rc 125 + a 'no such container'/'not running' marker), distinct from the inner
        command's own nonzero exit. Type that as SandboxUnavailableError so the session
        re-creates (carried lesson #1). NOTE: not live-re-verifiable (VM 202 destroyed);
        best-effort, mirrors the docker-py path's `_classify_failure`."""
        if rc == 0:
            return
        msg = err.decode("utf-8", "replace")
        if any(m in msg.lower() for m in _PODMAN_DEAD_MARKERS):
            raise SandboxUnavailableError(f"sandbox container died mid-session: {msg.strip()}")

    async def exec_shell(self, cmd: str, *, timeout_s: int) -> ExecResult:
        """Run `cmd` via the CLI native remote — the timeout is enforced in-container
        (`timeout` coreutil) with an outer subprocess backstop; a killed command is
        reported with `timed_out=True`, partial output preserved."""
        self._alive()
        argv = ["timeout", "-k", "5", str(timeout_s), "sh", "-c", cmd]
        rc, out, err = await asyncio.to_thread(self._exec, argv, timeout_s + 15)
        self._raise_if_dead(rc, err)
        return ExecResult(
            exit_code=rc,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            timed_out=rc in TIMEOUT_EXIT_CODES,
        )

    async def read_file(self, path: str) -> bytes:
        self._alive()
        target = self._container_path(path)
        rc, out, err = await asyncio.to_thread(self._exec, ["cat", "--", target], 60)
        if rc != 0:
            self._raise_if_dead(rc, err)
            raise SandboxError(f"read_file {path!r}: {err.decode('utf-8', 'replace').strip()}")
        return out

    async def list_dir(self, path: str) -> list[str]:
        self._alive()
        target = self._container_path(path)
        rc, out, err = await asyncio.to_thread(self._exec, ["ls", "-1A", "--", target], 30)
        if rc != 0:
            self._raise_if_dead(rc, err)
            raise SandboxError(f"list_dir {path!r}: {err.decode('utf-8', 'replace').strip()}")
        return sorted(n for n in out.decode("utf-8", "replace").splitlines() if n)

    async def write_file(self, path: str, data: bytes) -> None:
        """Write via podman-py `put_archive` (binary-safe + works over remote); the
        parent dir is created via the CLI first."""
        self._alive()
        target = self._container_path(path)
        parent = posixpath.dirname(target) or self._ws
        name = posixpath.basename(target)

        def _write() -> None:
            self._exec(["mkdir", "-p", "--", parent], 30)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                # [BP-00 root-cause] TarInfo defaults mtime to 0 (epoch 1970) — see
                # ContainerInstance.write_file; stamp the real write time.
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
            if not self._container.put_archive(parent, buf.getvalue()):
                raise SandboxError(f"write_file {path!r} failed")

        await asyncio.to_thread(_write)


class PodmanSandboxService:
    """[CONTRACT boundary] Creates Podman-backed instances over the native remote."""

    name = "podman"

    def __init__(
        self,
        config: SandboxConfig | None = None,
        *,
        client: Any | None = None,
        cli_runner: CliRunner | None = None,
    ) -> None:
        self._cfg = config or default_podman_config()
        self._injected_client = client  # test seam: a fake podman client
        self._cli_runner = cli_runner or _default_cli_runner  # test seam: a fake CLI
        self._client_cache: Any | None = None
        self._instances: dict[str, PodmanSandboxInstance] = {}

    @property
    def _cli_url(self) -> str:
        # podman-py uses `http+ssh://…`; the podman CLI wants `ssh://…`.
        return self._cfg.podman_url.removeprefix("http+")

    def _client(self) -> Any:
        """The podman-py client (lazy, cached) on the native remote socket. The SSH
        leg uses the system ssh client (tailnet keyless auth applies)."""
        if self._injected_client is not None:
            return self._injected_client
        if self._client_cache is None:
            try:
                from podman import PodmanClient

                client = PodmanClient(base_url=self._cfg.podman_url)
                client.ping()
            except Exception as exc:  # noqa: BLE001 — map to a typed infra error
                raise SandboxUnavailableError(
                    f"Podman unreachable at {self._cfg.podman_url}: {exc}"
                ) from exc
            self._client_cache = client
        return self._client_cache

    def _start_container(self, spec: SandboxSpec, instance_id: str) -> tuple[Any, str]:
        """The blocking Podman work for `create`, in a thread. Image-by-load (never
        pull); limits via the socket; typed errors on failure. Returns (container,
        name)."""
        client = self._client()

        try:
            present = client.images.exists(self._cfg.image)
        except Exception as exc:  # noqa: BLE001
            raise SandboxUnavailableError(f"could not query Podman images: {exc}") from exc
        if not present:
            raise SandboxUnavailableError(
                f"sandbox image {self._cfg.image!r} is not present (load it with "
                "`podman load`; this backend never pulls from a registry)"
            )

        mem_mb = spec.memory_mb or self._cfg.default_memory_mb
        cpu = spec.cpu or self._cfg.default_cpu
        vol_name = f"{self._cfg.workspace_volume_prefix}-{instance_id}"
        name = f"pmx-sbx-{instance_id}"

        try:
            client.volumes.create(name=vol_name)  # auto-created; persists past the container
            container = client.containers.create(
                image=self._cfg.image,
                command=["sleep", "infinity"],  # keepalive
                mem_limit=f"{mem_mb}m",
                cpu_quota=int(cpu * _CPU_PERIOD),
                cpu_period=_CPU_PERIOD,
                # FAIL-SAFE egress (the allowlisting proxy is wired for gVisor only so
                # far): only an explicit NETWORK capability ("open") gets raw bridge. A
                # filtered box (non-empty egress_allow) that we CAN'T yet enforce per-host
                # is SEALED — deny-all, never the old silent full-bridge false guarantee.
                # See gvisor.py _setup_filtered_egress; wiring this here is a live-verify
                # follow-up on the Podman host (VM 202).
                network_mode="bridge" if egress_mode(spec) == "open" else "none",
                volumes={vol_name: {"bind": self._cfg.container_workspace, "mode": "rw"}},
                environment={},  # NO host env leaks in
                working_dir=self._cfg.container_workspace,
                name=name,
                detach=True,
            )
            container.start()
            return container, name
        except SandboxUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
            from podman.errors import ImageNotFound

            if isinstance(exc, ImageNotFound):
                raise SandboxUnavailableError(
                    f"sandbox image {self._cfg.image!r} not found"
                ) from exc
            raise SandboxUnavailableError(f"container failed to start: {exc}") from exc

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance:
        instance_id = f"sbx_{uuid.uuid4().hex}"
        container, name = await asyncio.to_thread(self._start_container, spec, instance_id)
        instance = PodmanSandboxInstance(
            id=instance_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
            spec=spec,
            container=container,
            container_workspace=self._cfg.container_workspace,
            stop_timeout_s=self._cfg.stop_timeout_s,
            workspace_uid=self._cfg.workspace_uid,
            cli_url=self._cli_url,
            container_name=name,
            cli_runner=self._cli_runner,
        )
        self._instances[instance_id] = instance
        return instance

    async def get(self, instance_id: str) -> SandboxInstance | None:
        return self._instances.get(instance_id)
