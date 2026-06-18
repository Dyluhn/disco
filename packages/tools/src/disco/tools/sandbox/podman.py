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
import pathlib
import posixpath
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable
from typing import Any

from . import egress_proxy as _egress_proxy_mod
from ._container import (
    EGRESS_PROXY_PORT,
    TIMEOUT_EXIT_CODES,
    ContainerInstance,
    egress_mode,
    format_allow,
    proxy_env,
    proxy_run_argv,
)
from .base import ExecResult, SandboxError, SandboxInstance, SandboxSpec, SandboxUnavailableError
from .config import SandboxConfig, default_podman_config
from .naming import (
    EGR_NET_PREFIX,
    LABEL_CONV,
    LABEL_CONV_KEYS,
    SBX_NAME_PREFIX,
    SBX_NAME_PREFIXES,
    conv_id_from_labels,
)

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
        reload_timeout_s: float = 0.5,
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
            reload_timeout_s=reload_timeout_s,
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

    async def file_exists(self, path: str) -> bool:
        """[B4] Existence check via the CLI native remote (`test -f` in-container),
        matching read_file/list_dir's transport (podman-py exec is unusable over
        the remote API). False on a missing file or a jail-escaping path; a dead
        container is surfaced as a typed SandboxUnavailableError (→ recreate)."""
        self._alive()
        try:
            target = self._container_path(path)
        except SandboxError:
            return False
        rc, _out, err = await asyncio.to_thread(self._exec, ["test", "-f", target], 30)
        if rc != 0:
            self._raise_if_dead(rc, err)
        return rc == 0

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

    def _sidecar_cli_run(
        self, sidecar_name: str, argv: list[str], timeout: float
    ) -> tuple[int, bytes, bytes]:
        """[E8] One-shot CLI exec on the proxy SIDECAR. Reuses the same CLI runner
        as the sandbox (`self._cli_runner`) so the sidecar goes through the proven
        `podman --url ssh://…//socket exec` path — podman-py's native `exec_run`
        is broken over the remote API (per the backend's transport-split note in
        its module docstring), so the CLI is the only correct way to do one-shot
        commands on a remote podman container. The sandbox instance has a
        `self._exec` helper; the sidecar lives one container over, so we just
        call the runner directly with the sidecar's name."""
        return self._cli_runner(
            ["podman", "--url", self._cli_url, "exec", sidecar_name, *argv], timeout
        )

    def _setup_filtered_egress(
        self, client: Any, spec: SandboxSpec, instance_id: str, conversation_id: str = ""
    ) -> Any:
        """[E8 — live-verify on VM 202 pending] Stand up the allowlisting egress for
        a "filtered" box and return (network, sidecar, env, net_name).

        SPIRIT identical to `gvisor.py _setup_filtered_egress` — the OLD binary
        (none|bridge) silently gave an allowlisted box full network, which is the
        false guarantee E8 closes. Now an INTERNAL (no-NAT) network is the
        substrate: the box's ONLY route out is the proxy, and the proxy refuses
        any host the allowlist doesn't name. The HTTP(S)_PROXY env is belt-and-
        suspenders; the no-route property is the real guarantee.

        Three podman-specific notes (mirror gVisor's invariants #1/#2/#3 in
        gvisor.py; the podman host may not enforce the same boot-time netstack
        freeze as runsc, but we keep the create-on-bridge → connect-internal →
        start ordering so both NICs exist before any sandbox traffic):

        1. The proxy script is `put_archive`'d to the sidecar (no image rebuild;
           base image's `python3` runs it; honors the never-pull rule).
        2. `exec_run` on a podman-py remote container is broken (per the backend
           docstring), so the one-shot setup (resolv.conf + proxy launch) goes
           through the CLI runner, same as the sandbox's command path.
        3. The sandbox's proxy env names the sidecar by its INTERNAL-NET IP (read
           from the sidecar's attrs after `reload()`), with a name-fallback for
           the fake/test path."""
        net_name = f"{EGR_NET_PREFIX}{instance_id}"
        labels = {LABEL_CONV: conversation_id} if conversation_id else {}
        # The network carries the same label as the containers so the orphan
        # sweep can find it — its NAME is instance-keyed, not conversation-keyed.
        network = client.networks.create(net_name, internal=True, labels=labels)
        # (1) create on bridge → connect internal → start, so the sidecar's
        # `bridge` NIC (the route to the internet, the proxy's upstream) AND its
        # internal-net NIC BOTH exist before any sandbox traffic.
        sidecar = client.containers.create(
            image=self._cfg.image,
            command=["sh", "-c", "exec sleep infinity"],
            mem_limit="256m",
            detach=True,
            name=net_name,
            labels=labels,
        )
        network.connect(sidecar)
        sidecar.start()
        # (2) replace the dead embedded resolver with public DNS over the (working) route.
        self._sidecar_cli_run(
            sidecar.name,
            ["sh", "-c", 'printf "nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n" > /etc/resolv.conf'],
            15,
        )
        # Inject the proxy script (put_archive works over the podman remote, per
        # the backend docstring). Launch it in the background via the CLI runner
        # — podman-py's `exec_run` is broken, the CLI is the correct path.
        script = pathlib.Path(_egress_proxy_mod.__file__).read_bytes()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="egress_proxy.py")
            info.size = len(script)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(script))
        if not sidecar.put_archive("/", buf.getvalue()):
            raise SandboxUnavailableError("failed to inject egress proxy script into sidecar")
        allow = format_allow(spec.egress_allow)
        argv = proxy_run_argv(allow, EGRESS_PROXY_PORT)
        self._sidecar_cli_run(
            sidecar.name,
            ["sh", "-c", f"{' '.join(argv)} >/var/log/egress.log 2>&1 &"],
            10,
        )
        # (3) read the sidecar's IP on the internal net; the sandbox proxies by IP.
        try:
            sidecar.reload()
            nets = sidecar.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
            proxy_ip = nets.get(net_name, {}).get("IPAddress", "")
        except Exception:  # noqa: BLE001 — fake/test path or hung client
            proxy_ip = ""
        proxy_ip = proxy_ip or net_name  # fall back to the name (harmless for the fake/test path)
        env = proxy_env(proxy_ip, EGRESS_PROXY_PORT)
        return network, sidecar, env, net_name

    def _start_container(
        self, spec: SandboxSpec, instance_id: str, conversation_id: str = ""
    ) -> tuple[Any, str, Any, Any]:
        """The blocking Podman work for `create`, in a thread. Image-by-load (never
        pull); limits via the socket; typed errors on failure. Returns
        (container, name, egress_network, egress_sidecar) — the last two are
        non-None ONLY for a "filtered" box (the allowlisting proxy aux), None for
        sealed / open. The shared `ContainerInstance.destroy()` teardown walks
        the aux refs and tears them down (E8 wiring)."""
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
        name = f"{SBX_NAME_PREFIX}{instance_id}"

        # Per-mode network config — mirror gVisor's three-way posture. A filtered
        # box (E8) gets a PROXIED network (allowlist sidecar on an internal no-NAT
        # net), NEVER the old fail-safe seal. Only an explicit NETWORK capability
        # ("open") gets raw bridge; default remains deny-all.
        mode = egress_mode(spec)
        labels = {LABEL_CONV: conversation_id} if conversation_id else {}
        net_kwargs: dict[str, Any] = {}
        environment: dict[str, str] = {}
        egress_network = egress_sidecar = None
        if mode == "filtered":
            egress_network, egress_sidecar, environment, net_name = self._setup_filtered_egress(
                client, spec, instance_id, conversation_id
            )
            # podman-py's `containers.create` attaches user-defined networks via
            # `networks={name: per-net-config}`; the sandbox's ONLY interface
            # will be the internal no-NAT net (the proxy is the only route out).
            net_kwargs = {"networks": {net_name: {}}}
        elif mode == "open":
            net_kwargs = {"network_mode": "bridge"}  # explicit raw egress
        else:  # sealed
            net_kwargs = {"network_mode": "none"}

        try:
            client.volumes.create(name=vol_name)  # auto-created; persists past the container
            container = client.containers.create(
                image=self._cfg.image,
                command=["sleep", "infinity"],  # keepalive
                mem_limit=f"{mem_mb}m",
                cpu_quota=int(cpu * _CPU_PERIOD),
                cpu_period=_CPU_PERIOD,
                **net_kwargs,
                volumes={vol_name: {"bind": self._cfg.container_workspace, "mode": "rw"}},
                # NO host env leaks in. For a filtered box the proxy routing vars
                # (defense in depth atop the no-route network) are passed — that's
                # the ONLY exception.
                environment=environment,
                working_dir=self._cfg.container_workspace,
                name=name,
                labels=labels,
                detach=True,
            )
            container.start()
            return container, name, egress_network, egress_sidecar
        except SandboxUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 — start failure, real cause preserved
            from podman.errors import ImageNotFound

            if isinstance(exc, ImageNotFound):
                raise SandboxUnavailableError(
                    f"sandbox image {self._cfg.image!r} not found"
                ) from exc
            # Don't leak the egress aux if the sandbox itself failed to start
            # (E8: the parent class's teardown walks these refs).
            if egress_sidecar is not None:
                try:
                    egress_sidecar.remove(force=True)
                except Exception:  # noqa: BLE001 — best-effort
                    pass
            if egress_network is not None:
                try:
                    egress_network.remove()
                except Exception:  # noqa: BLE001 — best-effort
                    pass
            raise SandboxUnavailableError(f"container failed to start: {exc}") from exc

    async def create(
        self, spec: SandboxSpec, *, owner_id: str, conversation_id: str
    ) -> SandboxInstance:
        instance_id = f"sbx_{uuid.uuid4().hex}"
        container, name, egress_network, egress_sidecar = await asyncio.to_thread(
            self._start_container, spec, instance_id, conversation_id
        )
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
            # Wedge-guard timeout (Dispo #25, E5 wiring) — untouched in E8.
            reload_timeout_s=self._cfg.reload_timeout_s,
        )
        # E8: attach the filtered-egress aux so the inherited ContainerInstance
        # teardown tears down the proxy sidecar + internal network. For sealed /
        # open these are None (the defaults on the class), so the teardown is a
        # no-op.
        instance._egress_network = egress_network
        instance._egress_sidecar = egress_sidecar
        self._instances[instance_id] = instance
        return instance

    async def get(self, instance_id: str) -> SandboxInstance | None:
        return self._instances.get(instance_id)

    async def list_live_instances(self) -> list[str]:
        """Return conversation_ids of all live pmx-sbx-* containers on this Podman backend.

        Side effect (documented, deliberate — mirrors the gVisor backend): an
        unlabeled pmx-sbx-* container predates the label scheme and cannot be
        correlated to any conversation — unreachable garbage by construction,
        reaped on sight."""
        def _list() -> list[str]:
            try:
                client = self._client()
                # Dual-read: sweep BOTH the current `disco-sbx-` and legacy
                # `pmx-sbx-` name prefixes so a rename never strands a container.
                seen_ids: set[str] = set()
                containers = []
                for prefix in SBX_NAME_PREFIXES:
                    for c in client.containers.list(filters={"name": prefix}):
                        if c.id not in seen_ids:
                            seen_ids.add(c.id)
                            containers.append(c)
                result = []
                for c in containers:
                    cid = conv_id_from_labels(getattr(c, "labels", None))
                    if cid:
                        result.append(cid)
                        continue
                    try:  # legacy/unlabeled: reap on sight (see docstring)
                        c.stop(timeout=2)
                        c.remove(force=True)
                    except Exception:  # noqa: BLE001 — best-effort
                        pass
                return result
            except Exception:  # noqa: BLE001 — Podman unreachable: non-fatal
                return []
        return await asyncio.to_thread(_list)

    async def destroy_by_conversation(self, conversation_id: str) -> None:
        """Destroy all disco-sbx-*/pmx-sbx-* containers labelled with this conversation_id."""
        def _destroy() -> None:
            try:
                client = self._client()
                # Dual-read: match BOTH the current and legacy conversation label
                # keys (union, dedup) so an old-scheme container is still removed.
                seen_ids: set[str] = set()
                for key in LABEL_CONV_KEYS:
                    for c in client.containers.list(
                        all=True,
                        filters={"label": f"{key}={conversation_id}"},
                    ):
                        if c.id in seen_ids:
                            continue
                        seen_ids.add(c.id)
                        try:
                            c.stop(timeout=2)
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            c.remove(force=True)
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001 — best-effort
                pass
        await asyncio.to_thread(_destroy)
