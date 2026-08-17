"""gVisor sandbox backend — hermetic (mocked Docker SDK), tool-sandbox §5/§7.

Proves the create/exec/close session model, capability-set network sealing,
resource limits, timeout reporting, the typed infra-error paths, and the headline
secret non-leak (the backend passes NO host env to the container) — all offline.
The real isolation (gVisor engages, sealing actually blocks the net) is the live
VM-201 check; here we prove the backend drives Docker correctly.
"""

from __future__ import annotations

import subprocess
import sys
from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest
from disco.tools.anatomy import Capability
from disco.tools.sandbox import (
    GvisorSandboxService,
    SandboxConfig,
    SandboxError,
    SandboxSpec,
    SandboxUnavailableError,
)
from disco.tools.sandbox._container import (
    INTERNAL_PORTS,
    bounded_delete_argv,
    bounded_delete_result,
    loopback_port_bindings,
)
from disco.tools.sandbox.base import SandboxPermissionError
from docker.errors import ImageNotFound

_Exec = namedtuple("_Exec", ["exit_code", "output"])


def test_sandbox_image_browser_bundle_is_shared_with_uid_1000_and_marp() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[4] / "current" / "deploy" / "sandbox" / "Dockerfile"
    ).read_text()

    shared_path = "ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright"
    browser_install = "playwright install --with-deps chromium"
    stable_executable = "/usr/local/bin/playwright-chromium"

    assert shared_path in dockerfile
    assert dockerfile.index(shared_path) < dockerfile.index(browser_install)
    assert 'chromium_path="$(find "$PLAYWRIGHT_BROWSERS_PATH"' in dockerfile
    assert 'chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"' in dockerfile
    assert f'ln -sf "$chromium_path" {stable_executable}' in dockerfile
    assert f"ENV CHROME_PATH={stable_executable}" in dockerfile
    assert "useradd -m -u 1000" in dockerfile
    assert "/root/.cache/ms-playwright" not in dockerfile


def test_sandbox_image_includes_asset_inspection_clis() -> None:
    """H204: the canonical sandbox must ship the asset-inspection commands agents use.

    RUN-358 proved that advertising a common-CLI image while omitting ``file`` and
    ``xxd`` produces repeated exit-127 thrash. Keep this assertion bound to the
    specific common-CLI install layer so mentioning a package in prose cannot pass.
    """
    dockerfile = (
        Path(__file__).resolve().parents[4] / "current" / "deploy" / "sandbox" / "Dockerfile"
    ).read_text()
    common_cli_section = dockerfile.split("# Build toolchain + common CLIs", 1)[1].split(
        "# noVNC live-browser stack", 1
    )[0]
    install_command = common_cli_section.split("RUN apt-get update", 1)[1].split("&& rm -rf", 1)[0]
    install_tokens = set(install_command.replace("\\", " ").split())

    assert {"file", "xxd"} <= install_tokens


class FakeContainer:
    def __init__(self, run_kwargs: dict) -> None:
        self.run_kwargs = run_kwargs
        self.stopped = False
        self.removed = False
        self.started = False
        self.status = "created"
        self.attrs: dict = {}  # NetworkSettings populated on reload() if needed
        self.exec_calls: list[list[str]] = []
        # queued (exit_code, stdout_bytes, stderr_bytes) for shell execs; else echoes ok
        self.exec_results: list[tuple[int, bytes, bytes]] = []
        self.fs: dict[str, bytes] = {}  # in-container files, by absolute path
        self.tar_mtimes: dict[str, int] = {}  # mtime of each put_archive'd member
        self.put_archive_paths: list[str] = []
        self.deny_rename_over: set[str] = set()

    def start(self):
        self.started = True
        self.status = "running"

    def reload(self):  # docker-py refreshes .attrs; the fake leaves them empty
        pass

    def exec_run(self, cmd, demux=False, workdir=None, detach=False):
        self.exec_calls.append(cmd)
        if detach:
            return _Exec(0, (b"", b"") if demux else b"")
        # file ops the backend issues: cat / ls / mkdir against the tiny FS
        if cmd[0] == "python3" and "DISCO_READ_" in cmd[2]:
            path = cmd[3].rstrip("/") + "/" + cmd[4]
            if path in self.fs:
                code, out, err = 0, self.fs[path], b""
            else:
                code, out, err = 44, b"", b"DISCO_READ_MISSING:no file"
        elif cmd[0] == "python3" and "DISCO_DELETE_" in cmd[2]:
            path = cmd[3].rstrip("/") + "/" + cmd[4]
            if path in self.fs:
                self.fs.pop(path)
                self.tar_mtimes.pop(path, None)
                code, out, err = 0, b"", b""
            else:
                code, out, err = 44, b"", b"DISCO_DELETE_MISSING:no file"
        elif cmd[0] == "realpath":
            code, out, err = 0, f"{cmd[-1]}\n".encode(), b""
        elif cmd[0] == "cat":
            path = cmd[-1]
            if path in self.fs:
                code, out, err = 0, self.fs[path], b""
            else:
                code, out, err = 1, b"", b"cat: No such file"
        elif cmd[0] == "ls":
            prefix = cmd[-1].rstrip("/") + "/"
            names = sorted(
                {p[len(prefix) :].split("/")[0] for p in self.fs if p.startswith(prefix)}
            )
            code, out, err = 0, ("\n".join(names) + "\n").encode() if names else b"", b""
        elif cmd[0] == "mkdir":
            code, out, err = 0, b"", b""
        elif cmd[0] == "test":
            path = cmd[-1]
            code, out, err = (0, b"", b"") if path in self.fs else (1, b"", b"")
        elif cmd[0] == "cp":
            source, target = cmd[-2:]
            if source in self.fs:
                self.fs[target] = self.fs[source]
                if source in self.tar_mtimes:
                    self.tar_mtimes[target] = self.tar_mtimes[source]
                code, out, err = 0, b"", b""
            else:
                code, out, err = 1, b"", b"missing source"
        elif cmd[0] == "mv":
            source, target = cmd[-2:]
            if target in self.deny_rename_over and target in self.fs:
                code, out, err = 1, b"", b"permission denied"
            elif source in self.fs:
                self.fs[target] = self.fs.pop(source)
                if source in self.tar_mtimes:
                    self.tar_mtimes[target] = self.tar_mtimes.pop(source)
                code, out, err = 0, b"", b""
            else:
                code, out, err = 1, b"", b"missing source"
        elif cmd[0] == "rm":
            self.fs.pop(cmd[-1], None)
            self.tar_mtimes.pop(cmd[-1], None)
            code, out, err = 0, b"", b""
        elif cmd[0] == "sha256sum":
            import hashlib

            path = cmd[-1]
            if path in self.fs:
                digest = hashlib.sha256(self.fs[path]).hexdigest()
                code, out, err = 0, f"{digest}  {path}\n".encode(), b""
            else:
                code, out, err = 1, b"", b"missing file"
        elif cmd[0] == "stat":
            path = cmd[-1]
            code, out, err = (
                (0, f"{len(self.fs[path])}\n".encode(), b"")
                if path in self.fs
                else (1, b"", b"stat: No such file")
            )
        elif self.exec_results:
            code, out, err = self.exec_results.pop(0)
        else:
            code, out, err = 0, b"ok\n", b""
        return _Exec(code, (out, err) if demux else (out or b"") + (err or b""))

    def put_archive(self, path, data):
        import io
        import tarfile

        self.put_archive_paths.append(path)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for m in tar.getmembers():
                f = tar.extractfile(m)
                self.fs[path.rstrip("/") + "/" + m.name] = f.read() if f else b""
                self.tar_mtimes[path.rstrip("/") + "/" + m.name] = m.mtime
        return True

    def stop(self, timeout=None):
        self.stopped = True
        self.status = "exited"

    def remove(self, force=False):
        self.removed = True


class _FakeImages:
    """Stands in for client.images — the never-pull guard calls .get() before any run."""

    def __init__(self, has: bool) -> None:
        self._has = has

    def get(self, tag):
        if not self._has:
            raise ImageNotFound(f"no such image: {tag}")
        return object()  # an opaque image handle is enough


class FakeVolume:
    def __init__(self, name: str, labels: dict[str, str], options: dict[str, Any]) -> None:
        self.name = name
        self.labels = labels
        self.options = options
        self.removed = False

    def remove(self, force=False):
        self.removed = True


class _FakeVolumes:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.objects: dict[str, FakeVolume] = {}

    def create(self, name, labels=None, **options):
        self.created.append(name)
        volume = FakeVolume(name, labels or {}, options)
        self.objects[name] = volume
        return volume

    def list(self, filters=None):
        label = (filters or {}).get("label")
        if not label:
            return list(self.objects.values())
        key, _, value = label.partition("=")
        return [volume for volume in self.objects.values() if volume.labels.get(key) == value]


class FakeNetwork:
    """Stands in for a docker-py Network (filtered-egress internal network)."""

    def __init__(self, name: str, **attrs: Any) -> None:
        self.name = name
        self.attrs = attrs
        self.connected: list[Any] = []  # containers connected, with aliases
        self.removed = False

    def connect(self, container, aliases=None):
        self.connected.append((container, aliases))
        container.attrs = {
            "NetworkSettings": {"Networks": {self.name: {"IPAddress": "172.28.0.2"}}}
        }

    def remove(self):
        self.removed = True


class _FakeNetworks:
    def __init__(self) -> None:
        self.created: list[FakeNetwork] = []

    def create(self, name, **kwargs):
        net = FakeNetwork(name, **kwargs)
        self.created.append(net)
        return net


class FakeDockerClient:
    def __init__(
        self,
        *,
        runtimes=("runsc", "runc"),
        run_error: Exception | None = None,
        has_image: bool = True,
    ) -> None:
        self._runtimes = {r: {} for r in runtimes}
        self._run_error = run_error
        self.containers = self
        self.images = _FakeImages(has_image)
        self.volumes = _FakeVolumes()
        self.networks = _FakeNetworks()
        self.runs: list[FakeContainer] = []  # every container started (sidecar + sandbox)
        self.last: FakeContainer | None = None

    def ping(self):
        return True

    def info(self):
        return {"Runtimes": self._runtimes}

    def run(self, **kwargs):  # client.containers.run(**kwargs)
        if self._run_error is not None:
            raise self._run_error
        c = FakeContainer(kwargs)
        # [FIX6] mirror reality: a sandbox joined to the internal egress net is
        # assigned an IP on it. `_launch_inbound_forwarder` reads this after
        # reload() to target the inbound TCP forwarder in every container mode.
        net = kwargs.get("network")
        if net:
            c.attrs = {"NetworkSettings": {"Networks": {net: {"IPAddress": "172.28.0.5"}}}}
        self.runs.append(c)
        c.status = "running"
        self.last = c
        return c

    def create(self, **kwargs):  # client.containers.create(**kwargs) — the egress sidecar
        c = FakeContainer(kwargs)
        self.runs.append(c)
        return c  # NOT self.last: the sandbox (via run) stays the asserted-on container


def _svc(tmp_path, client: Any) -> GvisorSandboxService:
    cfg = SandboxConfig(workspace_root=str(tmp_path))
    return GvisorSandboxService(cfg, client=client)


async def test_create_exec_close_session_model(tmp_path):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    # create started ONE keepalive container (not one-per-command)
    kw = client.last.run_kwargs
    assert kw["command"] == ["sleep", "infinity"]
    assert kw["runtime"] == "runsc"
    assert kw["image"] == "disco-sandbox:base"
    # multiple execs into the SAME container (session, not one-shot)
    r1 = await inst.exec_shell("echo hi", timeout_s=10)
    await inst.exec_shell("echo bye", timeout_s=10)
    assert r1.exit_code == 0 and r1.stdout == "ok\n" and r1.timed_out is False
    assert len(client.last.exec_calls) == 2  # same container, two execs
    # A guest-side streaming helper bounds output before it crosses the daemon API.
    assert client.last.exec_calls[0][:3] == ["python3", "-c", client.last.exec_calls[0][2]]
    assert "echo hi" in client.last.exec_calls[0]
    # close stops + removes the container
    await inst.destroy()
    assert client.last.stopped and client.last.removed
    # use-after-destroy is rejected
    with pytest.raises(SandboxError):
        await inst.exec_shell("echo", timeout_s=5)


async def test_sealed_control_sidecar_start_failure_is_typed_and_leak_free(tmp_path, monkeypatch):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)

    def fail_start(_self):
        raise RuntimeError("raw daemon transport detail")

    monkeypatch.setattr(FakeContainer, "start", fail_start)
    with pytest.raises(SandboxUnavailableError) as raised:
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")

    assert str(raised.value) == "inbound control sidecar setup failed; refusing sandbox start"
    assert "raw daemon" not in str(raised.value)
    assert len(client.runs) == 1 and client.runs[0].removed
    assert len(client.networks.created) == 1 and client.networks.created[0].removed
    assert client.volumes.created == []


@pytest.mark.parametrize("reload_behavior", ["missing", "raise", "stopped"])
async def test_policy_sidecar_requires_a_verified_internal_ip(
    tmp_path, monkeypatch, reload_behavior
):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)

    def hostile_reload(container):
        if reload_behavior == "raise":
            raise RuntimeError("raw daemon reload detail")
        if reload_behavior == "stopped":
            container.status = "exited"
        else:
            container.attrs = {}

    monkeypatch.setattr(FakeContainer, "reload", hostile_reload)
    with pytest.raises(
        SandboxUnavailableError,
        match="policy sidecar internal address is unavailable",
    ) as raised:
        await svc.create(
            SandboxSpec(egress_allow=frozenset({"api.example.com"})),
            owner_id="o",
            conversation_id="c",
        )

    assert "raw daemon" not in str(raised.value)
    assert len(client.runs) == 1 and client.runs[0].removed
    assert len(client.networks.created) == 1 and client.networks.created[0].removed
    assert client.volumes.created == []


async def test_sealed_by_default_filtered_on_allowlist_open_on_capability(tmp_path):
    # The THREE-way egress posture (the fix for the old none|bridge binary that
    # silently gave an allowlisted box full network).
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    # 1) default spec → SEALED guest on an internal no-NAT network. The
    # control sidecar has no proxy/env and publishes only the kernel port.
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    sealed_sidecar, sealed_sandbox = client.runs[:2]
    sealed_net = client.networks.created[0]
    assert sealed_net.attrs["internal"] is True
    assert sealed_sandbox.run_kwargs["network"] == sealed_net.name
    assert "network_mode" not in sealed_sandbox.run_kwargs
    assert sealed_sandbox.run_kwargs["environment"] == {}
    assert sealed_sidecar.run_kwargs["ports"] == loopback_port_bindings(INTERNAL_PORTS)
    assert "/egress_proxy.py" not in sealed_sidecar.fs
    # 2) egress allowlist → FILTERED (proxy enforces the list) — NOT raw bridge.
    await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})), owner_id="o", conversation_id="c"
    )
    sandbox_kw = client.last.run_kwargs
    assert "network_mode" not in sandbox_kw  # NOT full bridge!
    assert sandbox_kw["network"].startswith("disco-egr-")  # the internal no-NAT net
    assert sandbox_kw["environment"]["HTTPS_PROXY"] == "http://172.28.0.2:8888"
    # 3) legacy NETWORK capability → public-only proxy, never a raw bridge.
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})), owner_id="o", conversation_id="c"
    )
    assert client.last.run_kwargs["network"].startswith("disco-egr-")
    assert client.networks.created[-1].attrs.get("internal") is True
    launch = next(
        call
        for call in client.runs[-2].exec_calls
        if any("egress_proxy.py" in str(arg) for arg in call)
    )
    assert "--public-only" in " ".join(launch)


async def test_filtered_egress_stands_up_and_tears_down_proxy_sidecar(tmp_path):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    inst = await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com", ".pypi.org"})),
        owner_id="o",
        conversation_id="c",
    )
    # An INTERNAL (no-NAT) network was created — the containment substrate.
    assert len(client.networks.created) == 1
    net = client.networks.created[0]
    assert net.attrs.get("internal") is True
    # A proxy SIDECAR exists (created before the sandbox) and joined the net.
    assert len(client.runs) == 2  # sidecar + sandbox
    sidecar = client.runs[0]
    # VERIFIED-LIVE invariant #1: the sidecar is create→connect→START (NOT run +
    # hot-connect) so gVisor sees both NICs at boot. So it must be explicitly
    # started, and joined to the net BEFORE that.
    assert sidecar.started is True
    assert net.connected and net.connected[0][0] is sidecar
    # VERIFIED-LIVE invariant #2: a working resolver replaces the dead embedded
    # 127.0.0.11 (else the proxy can't resolve any upstream).
    resolv = [c for c in sidecar.exec_calls if any("resolv.conf" in str(a) for a in c)]
    assert resolv, "sidecar resolv.conf was not repointed to a public resolver"
    assert any("1.1.1.1" in str(a) for a in resolv[0])
    # The stdlib proxy script was injected and launched with the allowlist.
    assert any("egress_proxy.py" in p for p in sidecar.fs)
    launched = [c for c in sidecar.exec_calls if any("egress_proxy.py" in str(a) for a in c)]
    assert launched, "proxy was not launched in the sidecar"
    assert launched[0][:2] == ["sh", "-c"]
    assert launched[0][2].startswith("exec python3 /egress_proxy.py ")
    assert "&" not in launched[0][2]
    joined = " ".join(launched[0])
    assert "api.example.com" in joined and ".pypi.org" in joined
    # destroy() tears down BOTH the sandbox and the egress aux (no orphans).
    await inst.destroy()
    assert sidecar.removed and net.removed


async def test_sealed_host_service_box_gets_only_internal_relay_network(tmp_path):
    client = FakeDockerClient()
    cfg = SandboxConfig(
        workspace_root=str(tmp_path),
        host_service_upstream="https://agent.internal:8443",
    )
    svc = GvisorSandboxService(cfg, client=client)
    inst = await svc.create(SandboxSpec(host_services=True), owner_id="o", conversation_id="c")
    sidecar, sandbox = client.runs
    net = client.networks.created[0]
    assert net.attrs["internal"] is True
    assert sandbox.run_kwargs["network"] == net.name
    assert "network_mode" not in sandbox.run_kwargs
    # Sealed means the sidecar publishes only the internal control port; no user
    # preview is mapped. The relay remains the one narrow outbound capability.
    assert sidecar.run_kwargs["ports"] == loopback_port_bindings(INTERNAL_PORTS)
    proxy_launch = next(c for c in sidecar.exec_calls if "egress_proxy.py" in " ".join(c))
    assert "--allow ''" in " ".join(proxy_launch)
    assert "/capability_relay.py" in sidecar.fs
    relay_launch = next(c for c in sidecar.exec_calls if "capability_relay.py" in " ".join(c))
    assert relay_launch[:2] == ["sh", "-c"]
    assert relay_launch[2].startswith("exec python3 /capability_relay.py ")
    assert "&" not in relay_launch[2]
    assert inst.host_service_relay_url == "http://172.28.0.2:3211"
    await inst.destroy()
    assert sidecar.removed and net.removed


async def test_required_host_service_relay_readiness_failure_aborts_and_cleans(tmp_path):
    class BrokenRelayContainer(FakeContainer):
        def exec_run(self, cmd, demux=False, workdir=None, detach=False):
            if cmd[:2] == ["python3", "-c"] and cmd[-1] == "3211":
                return _Exec(1, (b"", b"") if demux else b"")
            return super().exec_run(cmd, demux=demux, workdir=workdir, detach=detach)

    class BrokenRelayClient(FakeDockerClient):
        def create(self, **kwargs):
            container = BrokenRelayContainer(kwargs)
            self.runs.append(container)
            return container

    client = BrokenRelayClient()
    cfg = SandboxConfig(
        workspace_root=str(tmp_path),
        host_service_upstream="https://agent.internal:8443",
    )
    svc = GvisorSandboxService(cfg, client=client)
    with pytest.raises(SandboxUnavailableError, match="relay failed readiness"):
        await svc.create(SandboxSpec(host_services=True), owner_id="o", conversation_id="c")
    assert client.runs[0].removed
    assert client.networks.created[0].removed
    assert client.last is None  # sandbox never started


async def test_container_relay_rejects_sidecar_loopback_http_before_resources(tmp_path):
    client = FakeDockerClient()
    cfg = SandboxConfig(
        workspace_root=str(tmp_path),
        host_service_upstream="http://127.0.0.1:8000",
    )
    svc = GvisorSandboxService(cfg, client=client)
    with pytest.raises(SandboxUnavailableError, match="sidecar loopback"):
        await svc.create(SandboxSpec(host_services=True), owner_id="o", conversation_id="c")
    assert client.runs == [] and client.networks.created == []


async def test_resource_limits_and_workspace_mount_applied(tmp_path):
    client = FakeDockerClient()
    # EPIC H (P1): the deployment config is the MAXIMUM. cpu=2.0 / memory_mb=512 are WITHIN
    # this deployment's ceiling (default_cpu=4.0, default_memory_mb=2048), so they flow
    # through unchanged — proving the limits reach the create call. (Above-max clamping is
    # the separate test_spec_cannot_loosen_bounds_above_config_max regression.)
    cfg = SandboxConfig(workspace_root=str(tmp_path), default_cpu=4.0, workspace_uid=1234)
    svc = GvisorSandboxService(cfg, client=client)
    await svc.create(SandboxSpec(cpu=2.0, memory_mb=512), owner_id="o", conversation_id="c")
    kw = client.last.run_kwargs
    assert kw["mem_limit"] == "512m"
    assert kw["nano_cpus"] == 2_000_000_000
    # EPIC H host-protection: the create call carries a pids cap (cgroup pids.max). The
    # spec left `pids` unset → the backend's config default (512) is applied.
    assert kw["pids_limit"] == 512
    # F02: mount ownership alone is insufficient; the guest write principal must
    # match it or atomic scaffold writes become root-owned.
    assert kw["user"] == "1234:1234"
    # workspace is an ephemeral, size-capped tmpfs volume mounted at /workspace.
    (volume_name,) = kw["volumes"].keys()
    assert kw["volumes"][volume_name] == {"bind": "/workspace", "mode": "rw"}
    volume = client.volumes.objects[volume_name]
    assert volume.options["driver"] == "local"
    assert "size=4096m" in volume.options["driver_opts"]["o"]
    assert "uid=1234,gid=1234" in volume.options["driver_opts"]["o"]


async def test_pids_limit_spec_override_and_config_default(tmp_path):
    # EPIC H: a SandboxSpec MAY tighten the pids cap; otherwise the config default bites.
    client = FakeDockerClient()
    cfg = SandboxConfig(workspace_root=str(tmp_path), default_pids_limit=256)
    svc = GvisorSandboxService(cfg, client=client)
    # spec leaves pids unset → config default (256)
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last.run_kwargs["pids_limit"] == 256
    # spec sets pids → it overrides the config default
    await svc.create(SandboxSpec(pids=64), owner_id="o", conversation_id="c")
    assert client.last.run_kwargs["pids_limit"] == 64


async def test_create_does_not_share_host_pid_namespace(tmp_path):
    # EPIC H escape suite (§9 b): the sandbox container must NOT join the host PID
    # namespace — it gets its OWN (Docker/runsc default when pid_mode is unset), so a
    # `kill <pid>` inside the box can only hit the box's own processes.
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    kw = client.last.run_kwargs
    assert kw.get("pid_mode") not in ("host",)  # never the host PID namespace
    assert "pid_mode" not in kw  # default (private) PID namespace, explicitly unset


async def test_no_host_env_leaks_into_the_box(tmp_path, monkeypatch):
    # THE HEADLINE: a secret in the agent-server's env must not reach the sandbox.
    monkeypatch.setenv("PMX_FAKE_SECRET", "sk-do-not-leak-1234")
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    # the backend passes an EMPTY env — nothing from os.environ is forwarded
    assert client.last.run_kwargs["environment"] == {}


async def test_timeout_is_reported_not_raised(tmp_path):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    client.last.exec_results = [(124, b"partial\n", b"")]  # `timeout` fired
    res = await inst.exec_shell("sleep 999", timeout_s=1)
    assert res.timed_out is True
    assert res.exit_code == 124
    assert res.stdout == "partial\n"  # partial output survives (not lost to a raise)


async def test_file_round_trip_and_escape_rejection_via_docker_exec(tmp_path):
    # File ops go through the container (exec/cp), NOT a host path — so they work
    # over Docker-over-SSH, not just when co-located.
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    await inst.write_file("sub/a.txt", b"hello")
    assert await inst.read_file("sub/a.txt") == b"hello"
    assert "sub" in await inst.list_dir(".")
    # workspace jail: escapes (../, absolute) are rejected before any Docker call
    with pytest.raises(SandboxError):
        await inst.read_file("../../etc/passwd")
    with pytest.raises(SandboxError):
        await inst.write_file("/etc/evil", b"x")


async def test_write_file_stamps_real_mtime(tmp_path):
    # [BP-00 root-cause regression] TarInfo defaults mtime to 0 (epoch 1970) and
    # put_archive preserves it: every agent write then lands "older" than any
    # browser/build cache, so If-Modified-Since answers 304 forever and the
    # preview shows the FIRST version of an edited file for the rest of the run.
    import time as _time

    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    before = int(_time.time())
    await inst.write_file("site/style.css", b"body { color: #111; }")
    (mtime,) = [v for k, v in client.last.tar_mtimes.items() if k.endswith("style.css")]
    assert mtime >= before, f"tar member mtime {mtime} is stale (epoch-1970 regression)"


async def test_atomic_write_stages_archive_outside_workspace_then_guest_renames(tmp_path):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")

    await inst.atomic_write("site/index.html", b"<h1>atomic</h1>")

    assert client.last.fs["/workspace/site/index.html"] == b"<h1>atomic</h1>"
    assert client.last.put_archive_paths[-1] == "/tmp"
    assert not any("disco-upload-" in path for path in client.last.fs)
    assert not any(".disco-tmp-" in path for path in client.last.fs)


async def test_delete_file_removes_the_resolved_guest_file(tmp_path):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    await inst.write_file("site/stale.html", b"stale")

    await inst.delete_file("site/stale.html")

    assert "/workspace/site/stale.html" not in client.last.fs
    delete_calls = [
        call
        for call in client.last.exec_calls
        if call[0] == "python3" and "DISCO_DELETE_" in call[2]
    ]
    assert delete_calls
    assert delete_calls[-1][3:] == ["/workspace", "site/stale.html"]


async def test_delete_file_does_not_re_resolve_path_after_lexical_jail_check(tmp_path, monkeypatch):
    """The guest helper owns the no-follow check+unlink; no second realpath race exists."""
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="delete-race")
    await inst.write_file("site/stale.html", b"stale")
    client.last.exec_calls.clear()

    def _unexpected_realpath(_path):
        raise AssertionError("delete_file must not resolve a mutable path twice")

    monkeypatch.setattr(inst, "_guest_realpath", _unexpected_realpath)
    await inst.delete_file("site/stale.html")

    assert "/workspace/site/stale.html" not in client.last.fs
    assert not any(call[0] == "rm" for call in client.last.exec_calls)


@pytest.mark.parametrize("alias_kind", ["final", "parent"])
def test_guest_delete_helper_refuses_symlink_alias_and_preserves_outside_file(tmp_path, alias_kind):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_bytes(b"preserve-me")
    if alias_kind == "final":
        (workspace / "stale.txt").symlink_to(victim)
        relpath = "stale.txt"
    else:
        (workspace / "generated").symlink_to(outside, target_is_directory=True)
        relpath = "generated/victim.txt"

    completed = subprocess.run(
        bounded_delete_argv(str(workspace), relpath, python=sys.executable),
        capture_output=True,
        check=False,
    )

    with pytest.raises(SandboxPermissionError):
        bounded_delete_result(relpath, completed.returncode, completed.stderr)
    assert victim.read_bytes() == b"preserve-me"


async def test_atomic_write_recovers_legacy_selinux_labeled_existing_file(tmp_path):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    target = "/workspace/site/index.html"
    client.last.fs[target] = b"legacy"
    client.last.deny_rename_over.add(target)

    await inst.atomic_write("site/index.html", b"replacement")

    assert client.last.fs[target] == b"replacement"
    assert client.last.put_archive_paths[-1] == "/workspace/site"
    assert not any("disco-upload-" in path for path in client.last.fs)
    assert not any(".disco-tmp-" in path for path in client.last.fs)


async def test_runsc_missing_is_a_typed_error(tmp_path):
    client = FakeDockerClient(runtimes=("runc",))  # gVisor not configured
    svc = _svc(tmp_path, client)
    with pytest.raises(SandboxUnavailableError, match="runsc"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")


async def test_image_missing_is_a_typed_error(tmp_path):
    # The never-pull guard: a missing image fails loud at images.get, before any run.
    client = FakeDockerClient(has_image=False)
    svc = _svc(tmp_path, client)
    with pytest.raises(SandboxUnavailableError, match="never pulls"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")


async def test_docker_unreachable_is_a_typed_error(tmp_path):
    # H027: a caller-owned UDS probe rejects this before DockerClient's failed
    # constructor can allocate and orphan its own Unix socket.
    cfg = SandboxConfig(workspace_root=str(tmp_path), docker_socket="unix:///nonexistent.sock")
    svc = GvisorSandboxService(cfg)
    with pytest.raises(SandboxUnavailableError, match="Docker unreachable"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")


def test_config_drives_host_details(tmp_path):
    # No hardcoded host details: a custom config flows into the run args.
    client = FakeDockerClient()
    cfg = SandboxConfig(
        workspace_root=str(tmp_path), runtime="runsc", image="custom:tag", container_workspace="/ws"
    )
    svc = GvisorSandboxService(cfg, client=client)

    async def _go():
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")

    import asyncio

    asyncio.run(_go())
    kw = client.last.run_kwargs
    assert kw["image"] == "custom:tag"
    assert kw["working_dir"] == "/ws"
    assert kw["volumes"][next(iter(kw["volumes"]))]["bind"] == "/ws"
