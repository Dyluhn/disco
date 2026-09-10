"""Podman sandbox backend — hermetic (mocked podman-py client + CLI runner).

Proves the session model, sealing, limits-in-create, NEVER-PULL, timeout reporting,
typed-error paths, file round-trip (put_archive write + CLI cat/ls read), and no
env leak — all offline. The real isolation + limits-bite-through-the-socket is live.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile

from types import SimpleNamespace
from typing import Any

import pytest
from disco.tools.anatomy import Capability
from disco.tools.sandbox import (
    PodmanSandboxService,
    SandboxConfig,
    SandboxError,
    SandboxSpec,
    SandboxUnavailableError,
)
from disco.tools.sandbox._container import (
    INTERNAL_PORTS,
    PUBLISHED_PORTS,
    loopback_port_bindings,
)
from disco.tools.sandbox.file_batch import SandboxFileMutation


def honest_host_config(create_kwargs: dict) -> dict[str, Any]:
    """The ``HostConfig`` a daemon that honours the create request would report."""
    mem = str(create_kwargs.get("mem_limit") or "0m").removesuffix("m")
    return {
        "Runtime": create_kwargs.get("runtime") or "",
        "Memory": int(float(mem or 0)) * 1024 * 1024,
        "CpuQuota": int(create_kwargs.get("cpu_quota") or 0),
        "CpuPeriod": int(create_kwargs.get("cpu_period") or 0),
        "PidsLimit": int(create_kwargs.get("pids_limit") or 0),
    }


class FakeContainer:
    def __init__(self, create_kwargs: dict, fs: dict[str, bytes]) -> None:
        self.create_kwargs = create_kwargs
        # A LYING engine: HostConfig fields to override on reload(). Podman's
        # Docker-compat API drops the requested runtime without erroring, so the
        # honest echo below is a property of the daemon, never a guarantee.
        self.host_config_lies: dict[str, Any] = {}
        self.fs = fs  # shared with the CLI runner (write here, read there)
        self.started = self.stopped = self.removed = False
        self.status = "created"
        self.attrs: dict = {}  # populated by reload() — E8 sidecar needs Networks
        # a queue of fake (exit_code, stdout, stderr) for exec_run; consumed FIFO.
        self.exec_results: list[tuple[int, bytes, bytes]] = []
        self.exec_calls: list[list[str]] = []
        self.remove_kwargs: dict | None = None
        self.put_calls: list[tuple[str, bytes]] = []
        # podman-py Container.name — the sidecar setup reads it to build the
        # `podman --url … exec <name> …` CLI argv. The fake's `name` follows
        # the create-kwarg convention (matches the real podman-py .name).
        self.name = create_kwargs.get("name", "")

    def start(self):
        self.started = True
        self.status = "running"

    def reload(self):
        """podman-py refreshes ``.attrs`` (callers set NetworkSettings); a TRUTHFUL
        engine also echoes the create request back under ``HostConfig``, which is
        where the post-create runtime and resource-limit guards read it.
        ``host_config_lies`` models an engine that does not."""
        host_config = honest_host_config(self.create_kwargs)
        host_config.update(self.host_config_lies)
        self.attrs["HostConfig"] = host_config

    def exec_run(self, cmd, demux=False, workdir=None, detach=False):
        # The sidecar's one-shot setup (resolv.conf + proxy launch) goes through
        # the CLI runner in the REAL podman backend (per E8 _sidecar_cli_run), so
        # the fake's podman-py `exec_run` path is only exercised by hermetic
        # tests that want to assert it. We accept the call and return a default
        # success — tests that need a specific exit/stdout push into exec_results.
        self.exec_calls.append(cmd)
        if self.exec_results:
            return self.exec_results.pop(0)
        return (0, b"", b"")

    def put_archive(self, path, data):
        self.put_calls.append((path, data))
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for m in tar.getmembers():
                f = tar.extractfile(m)
                self.fs[path.rstrip("/") + "/" + m.name] = f.read() if f else b""
        return True

    def stop(self, timeout=None):
        self.stopped = True
        self.status = "exited"

    def remove(self, force=False, v=False):
        self.removed = True
        self.remove_kwargs = {"force": force, "v": v}


class _Images:
    def __init__(self, has: bool) -> None:
        self._has = has
        self.pull_called = False

    def exists(self, tag):
        return self._has

    def pull(self, *a, **k):  # MUST never be called
        self.pull_called = True
        raise AssertionError("backend attempted a registry pull")


class _Volumes:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.objects: dict[str, FakeVolume] = {}

    def create(self, name, labels=None, **options):
        self.created.append(name)
        vol = FakeVolume(name, labels or {}, options)
        self.objects[name] = vol
        return vol

    def list(self, filters=None):
        label = (filters or {}).get("label")
        if not label:
            return list(self.objects.values())
        key, _, val = label.partition("=")
        return [vol for vol in self.objects.values() if vol.labels.get(key) == val]

    def get(self, name):
        return self.objects[name]


class FakeVolume:
    def __init__(self, name: str, labels: dict[str, str], options: dict) -> None:
        self.name = name
        self.id = "vol-" + name
        self.labels = labels
        self.options = options
        self.removed = False
        self.remove_kwargs: dict | None = None

    def remove(self, force=False):
        self.removed = True
        self.remove_kwargs = {"force": force}


class FakePodmanNetwork:
    """Mirrors podman-py Network: connect() joins a container, remove() tears it down."""

    def __init__(self, name: str, **attrs) -> None:
        self.name = name
        self.attrs = attrs
        self.connected: list = []
        self.removed = False

    def connect(self, container, aliases=None):
        self.connected.append((container, aliases))
        container.attrs = {"NetworkSettings": {"Networks": {self.name: {"IPAddress": "10.89.0.2"}}}}

    def remove(self):
        self.removed = True


class _FakeNetworks:
    def __init__(self) -> None:
        self.created: list[FakePodmanNetwork] = []

    def create(self, name, **kwargs):
        net = FakePodmanNetwork(name, **kwargs)
        self.created.append(net)
        return net


class FakePodmanClient:
    def __init__(
        self,
        *,
        has_image: bool = True,
        fs: dict | None = None,
        sandbox_ip: str = "10.89.0.7",
    ) -> None:
        self.images = _Images(has_image)
        self.volumes = _Volumes()
        self.networks = _FakeNetworks()
        self.containers = self
        self.fs = fs if fs is not None else {}
        self.last: FakeContainer | None = None
        self.created: list[FakeContainer] = []  # every container create()'d (sidecar + sandbox)
        self.sandbox_ip = sandbox_ip

    def ping(self):
        return True

    def create(self, **kwargs):
        c = FakeContainer(kwargs, self.fs)
        networks = kwargs.get("networks") or {}
        if kwargs.get("name", "").startswith("disco-sbx-") and networks and self.sandbox_ip:
            net_name = next(iter(networks))
            c.attrs = {"NetworkSettings": {"Networks": {net_name: {"IPAddress": self.sandbox_ip}}}}
        self.created.append(c)
        self.last = c
        return c


class FakeCli:
    """Stands in for native Podman exec, including ``exec --detach NAME``."""

    def __init__(self, fs: dict[str, bytes]) -> None:
        self.fs = fs
        self.results: list[tuple[int, bytes, bytes]] = []
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        container_index = 5 if argv[4] == "--detach" else 4
        rest = argv[container_index + 1 :]
        if rest[0] == "realpath":
            return (0, f"{rest[-1]}\n".encode(), b"")
        if rest[0] == "python3" and "DISCO_READ_" in rest[2]:
            path = rest[3].rstrip("/") + "/" + rest[4]
            return (
                (0, self.fs[path], b"")
                if path in self.fs
                else (44, b"", b"DISCO_READ_MISSING:no file")
            )
        if rest[0] == "cat":
            path = rest[-1]
            return (0, self.fs[path], b"") if path in self.fs else (1, b"", b"no file")
        if rest[0] == "ls":
            prefix = rest[-1].rstrip("/") + "/"
            names = sorted(
                {p[len(prefix) :].split("/")[0] for p in self.fs if p.startswith(prefix)}
            )
            return (0, ("\n".join(names) + "\n").encode() if names else b"", b"")
        if rest[0] == "mkdir":
            return (0, b"", b"")
        if rest[0] == "test":
            return (0, b"", b"") if rest[-1] in self.fs else (1, b"", b"")
        if rest[0] == "cp":
            source, target = rest[-2:]
            if source not in self.fs:
                return (1, b"", b"missing source")
            self.fs[target] = self.fs[source]
            return (0, b"", b"")
        if rest[0] == "mv":
            source, target = rest[-2:]
            if source not in self.fs:
                return (1, b"", b"missing source")
            self.fs[target] = self.fs.pop(source)
            return (0, b"", b"")
        if rest[0] == "rm":
            self.fs.pop(rest[-1], None)
            return (0, b"", b"")
        if rest[0] == "sha256sum":
            import hashlib

            path = rest[-1]
            if path not in self.fs:
                return (1, b"", b"missing file")
            digest = hashlib.sha256(self.fs[path]).hexdigest()
            return (0, f"{digest}  {path}\n".encode(), b"")
        if rest[0] == "stat":
            path = rest[-1]
            return (
                (0, f"{len(self.fs[path])}\n".encode(), b"")
                if path in self.fs
                else (1, b"", b"no file")
            )
        if self.results:
            return self.results.pop(0)
        return (0, b"ok\n", b"")


def _svc(
    has_image: bool = True, *, with_sandbox_ip: bool = True
) -> tuple[PodmanSandboxService, FakePodmanClient, FakeCli]:
    fs: dict[str, bytes] = {}
    client = FakePodmanClient(
        has_image=has_image,
        fs=fs,
        sandbox_ip="10.89.0.7" if with_sandbox_ip else "",
    )
    cli = FakeCli(fs)
    svc = PodmanSandboxService(
        SandboxConfig(backend="podman", runtime="crun"), client=client, cli_runner=cli
    )
    return svc, client, cli


async def test_create_exec_close_over_the_socket():
    svc, client, cli = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    kw = client.last.create_kwargs
    assert kw["command"] == ["sleep", "infinity"] and client.last.started is True
    # per-run named volume bound to /workspace
    (vol,) = client.volumes.created
    assert kw["volumes"][vol] == {"bind": "/workspace", "mode": "rw"}
    # exec goes through the CLI native remote (podman --url … exec)
    r = await inst.exec_shell("echo hi", timeout_s=10)
    assert r.stdout == "ok\n" and r.timed_out is False
    assert cli.calls[-1][:5] == ["podman", "--url", svc._cli_url, "exec", f"disco-sbx-{inst.id}"]
    assert cli.calls[-1][5:7] == ["python3", "-c"]
    assert "echo hi" in cli.calls[-1]
    await inst.destroy()
    assert client.last.stopped and client.last.removed
    assert client.last.remove_kwargs == {"force": True, "v": True}
    assert client.volumes.objects[vol].removed is True
    assert client.volumes.objects[vol].labels == {"disco.conversation_id": "c"}
    assert "size=4096m" in client.volumes.objects[vol].options["driver_opts"]["o"]


async def test_never_pulls_when_image_absent():
    svc, client, _cli = _svc(has_image=False)
    with pytest.raises(SandboxUnavailableError, match="never pulls"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.images.pull_called is False and client.last is None


async def test_sealed_control_sidecar_start_failure_is_typed_and_leak_free(monkeypatch):
    svc, client, _cli = _svc()

    def fail_start(_self):
        raise RuntimeError("raw daemon transport detail")

    monkeypatch.setattr(FakeContainer, "start", fail_start)
    with pytest.raises(SandboxUnavailableError) as raised:
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")

    assert str(raised.value) == "inbound control sidecar setup failed; refusing sandbox start"
    assert "raw daemon" not in str(raised.value)
    assert len(client.created) == 1 and client.created[0].removed
    assert len(client.networks.created) == 1 and client.networks.created[0].removed
    assert client.volumes.created == []


@pytest.mark.parametrize("reload_behavior", ["missing", "raise", "stopped"])
async def test_policy_sidecar_requires_a_verified_internal_ip(monkeypatch, reload_behavior):
    svc, client, _cli = _svc()

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
    assert len(client.created) == 1 and client.created[0].removed
    assert len(client.networks.created) == 1 and client.networks.created[0].removed
    assert client.volumes.created == []


async def test_sealed_default_open_when_granted():
    svc, client, _ = _svc()
    sealed = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    sealed_sidecar, sealed_sandbox = client.created[:2]
    sealed_net = client.networks.created[0]
    assert sealed_net.attrs["internal"] is True
    assert sealed_sandbox.create_kwargs["network_mode"] == "bridge"
    assert sealed_sandbox.create_kwargs["networks"] == {sealed_net.name: {}}
    assert sealed_sandbox.create_kwargs["environment"] == {}
    assert "ports" not in sealed_sandbox.create_kwargs
    assert sealed_sidecar.create_kwargs["ports"] == loopback_port_bindings(
        INTERNAL_PORTS, podman=True
    )
    assert sealed_sidecar.create_kwargs["cap_drop"] == ["ALL"]
    assert sealed_sidecar.create_kwargs["no_new_privileges"] is True
    assert sealed_sidecar.create_kwargs["sysctls"]["net.ipv4.ip_forward"] == "0"
    assert "/egress_proxy.py" not in client.fs
    assert sealed.expose_port(8000) is None
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})), owner_id="o", conversation_id="c"
    )
    assert client.last.create_kwargs["network_mode"] == "bridge"
    assert next(iter(client.last.create_kwargs["networks"])).startswith("disco-egr-")
    assert client.networks.created[-1].attrs.get("internal") is True


async def test_filtered_podman_spec_uses_proxied_egress_not_sealed():
    # E8 wiring: a filtered podman spec now gets a PROXIED network (allowlist
    # sidecar on an internal net) — NOT the old fail-safe `network_mode="none"`
    # seal that was gVisor-only. This is the regression guard for the local
    # podman backend; the formal E8 acceptance test (`test_e8_*.py`) is the
    # full new-test contract.
    svc, client, _ = _svc()
    await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})), owner_id="o", conversation_id="c"
    )
    # 1) NOT the old fail-safe seal (it's bridge-mode netns, not network_mode="none").
    assert client.last.create_kwargs.get("network_mode") != "none"
    # 2) the sandbox IS on the internal no-NAT net (proxied route out).
    networks = client.last.create_kwargs.get("networks") or {}
    assert networks and next(iter(networks)).startswith("disco-egr-")
    # 3) the allowlist proxy env is present (defense in depth atop the no-route net).
    env = client.last.create_kwargs["environment"]
    assert env["HTTPS_PROXY"].startswith("http://") and "8888" in env["HTTPS_PROXY"]


async def test_limits_and_no_env_leak_in_create(monkeypatch):
    monkeypatch.setenv("PMX_FAKE_SECRET", "sk-do-not-leak")
    # EPIC H (P1): config is the MAXIMUM. cpu=2.0 is within this deployment's ceiling
    # (default_cpu=4.0) so it flows through; above-max clamping is its own regression test.
    fs: dict[str, bytes] = {}
    client = FakePodmanClient(has_image=True, fs=fs)
    cli = FakeCli(fs)
    cfg = SandboxConfig(backend="podman", runtime="crun", default_cpu=4.0, workspace_uid=1234)
    svc = PodmanSandboxService(cfg, client=client, cli_runner=cli)
    await svc.create(SandboxSpec(cpu=2.0, memory_mb=512), owner_id="o", conversation_id="c")
    kw = client.last.create_kwargs
    assert kw["mem_limit"] == "512m"  # the limit goes through the socket create
    assert kw["cpu_quota"] == 200_000 and kw["cpu_period"] == 100_000
    # EPIC H host-protection: pids cap (cgroup pids.max) carried on the socket create.
    assert kw["pids_limit"] == 512  # spec unset → config default
    # F02: the sandbox itself must run as the same non-root principal that owns
    # the workspace volume.  The policy sidecar remains separately privileged.
    assert kw["user"] == "1234:1234"
    assert kw.get("pid_mode") != "host"  # own PID namespace, never the host's
    assert kw["environment"] == {}  # no host env into the box


async def test_timeout_reported_and_file_round_trip():
    svc, _client, cli = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    # staged binary-safe write -> read/list (CLI), jailed
    await inst.write_file("sub/a.txt", b"hello")
    assert await inst.read_file("sub/a.txt") == b"hello"
    assert "sub" in await inst.list_dir(".")
    with pytest.raises(SandboxError):
        await inst.read_file("../../etc/passwd")
    # timeout → reported (CLI returns 124), partial output preserved
    cli.results = [(124, b"partial\n", b"")]
    res = await inst.exec_shell("sleep 999", timeout_s=1)
    assert res.timed_out is True and res.exit_code == 124 and res.stdout == "partial\n"


async def test_workspace_archive_export_is_one_argv_only_stream(monkeypatch, tmp_path):
    from disco.tools.sandbox import podman as podman_module

    calls = []

    async def _archive(argv, destination, timeout):
        calls.append((argv, timeout))
        with tarfile.open(destination, "w:") as archive:
            data = b"<h1>ok</h1>"
            member = tarfile.TarInfo("index.html")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        destination.chmod(0o600)
        metadata = json.dumps({"skipped": [], "preserve": []}, separators=(",", ":"))
        return 0, (podman_module._WORKSPACE_EXPORT_META + metadata).encode()

    monkeypatch.setattr(podman_module, "_stream_workspace_archive", _archive)
    svc, _client, _cli = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    destination = tmp_path / "workspace.tar"

    assert await inst.export_workspace_archive(destination, max_depth=16, max_file_bytes=1024) == (
        [],
        [],
    )

    assert len(calls) == 1
    argv, _timeout = calls[0]
    assert argv[:7] == [
        "podman",
        "--url",
        svc._cli_url,
        "exec",
        "--workdir",
        "/",
        f"disco-sbx-{inst.id}",
    ]
    assert argv[7:10] == ["python3", "-I", "-c"]
    assert "sh" not in argv[:10]
    assert destination.stat().st_mode & 0o777 == 0o600
    with tarfile.open(destination, "r:") as archive:
        assert archive.getnames() == ["index.html"]


async def test_file_batch_uses_one_archive_upload_and_one_native_exec():
    svc, client, cli = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last is not None
    uploads_before = len(client.last.put_calls)
    calls_before = len(cli.calls)
    cli.results = [(0, b'{"version":1,"status":"ok","changes":[]}', b"")]

    result = await inst._commit_file_batch(
        tuple(SandboxFileMutation(f"file-{index}.txt", b"content") for index in range(18)),
        commit_last=("file-17.txt",),
    )

    assert result.changes == ()
    assert len(client.last.put_calls) == uploads_before + 1
    assert client.last.put_calls[-1][0] == "/tmp"
    assert len(cli.calls) == calls_before + 1
    argv = cli.calls[-1]
    assert argv[:5] == ["podman", "--url", svc._cli_url, "exec", f"disco-sbx-{inst.id}"]
    assert argv[5:8] == ["python3", "-I", "-c"]


def test_workspace_export_guest_filter_never_opens_runtime_secrets(tmp_path):
    from disco.tools.sandbox import podman as podman_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "index.html").write_bytes(b"ok")
    (workspace / ".env").write_bytes(b"SECRET=never-cross")
    (workspace / ".env.example").write_bytes(b"SECRET=replace-me")
    os.link(workspace / ".env", workspace / "innocent-alias.txt")
    # The real container's working directory is /workspace. Without isolated
    # mode this shadows stdlib tarfile before the secret filter even starts.
    (workspace / "tarfile.py").write_text("raise RuntimeError('workspace import executed')\n")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            podman_module._WORKSPACE_EXPORT_SCRIPT,
            str(workspace),
            "16",
            "1024",
        ],
        capture_output=True,
        check=True,
        cwd=workspace,
    )

    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        assert archive.getnames() == [".env.example", "index.html", "tarfile.py"]
        assert b"never-cross" not in result.stdout
    assert podman_module._WORKSPACE_EXPORT_META.encode() in result.stderr


def test_workspace_export_ancestor_swap_stays_on_open_directory(tmp_path):
    """A renamed ancestor cannot redirect later reads outside /workspace.

    The large first member fills the stdout pipe. Once its header arrives, the
    test atomically renames the directory and replaces its old path with a link.
    A path-based walker opens the later target through that link; descriptor-
    anchored traversal must keep reading from the already-open original dir.
    """

    from disco.tools.sandbox import podman as podman_module

    workspace = tmp_path / "workspace"
    victim = workspace / "victim"
    outside = tmp_path / "outside"
    victim.mkdir(parents=True)
    outside.mkdir()
    marker = b"M" * (512 * 1024)
    safe = b"safe-workspace-bytes"
    secret = b"OUTSIDE-ANCESTOR-SWAP-SECRET"
    (victim / "000-marker.bin").write_bytes(marker)
    (victim / "999-target.txt").write_bytes(safe)
    (outside / "999-target.txt").write_bytes(secret)

    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            podman_module._WORKSPACE_EXPORT_SCRIPT,
            str(workspace),
            "16",
            str(1024 * 1024),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=workspace,
        bufsize=0,
    )
    assert process.stdout is not None
    try:
        header = process.stdout.read(512)
        assert header[:100].rstrip(b"\0") == b"victim/000-marker.bin"
        original = workspace / "victim-original"
        victim.rename(original)
        victim.symlink_to(outside, target_is_directory=True)

        stdout_tail, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    output = header + stdout_tail
    assert process.returncode == 0, stderr.decode("utf-8", "replace")
    assert secret not in output
    with tarfile.open(fileobj=io.BytesIO(output), mode="r:") as archive:
        target = archive.extractfile("victim/999-target.txt")
        assert target is not None
        assert target.read() == safe


async def test_stream_workspace_archive_cancellation_reaps_and_removes_partial(tmp_path):
    from disco.tools.sandbox import podman as podman_module

    destination = tmp_path / "partial.tar"
    task = asyncio.create_task(
        podman_module._stream_workspace_archive(
            [sys.executable, "-c", "import time; time.sleep(30)"], destination, 60
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not destination.exists()


async def test_podman_unreachable_is_typed_error():
    # The native CLI probe rejects the endpoint BEFORE podman-py's failed UDS
    # connect can allocate-and-orphan a socket (H026).
    cfg = SandboxConfig(backend="podman", podman_url="http+unix:///nonexistent/podman.sock")
    svc = PodmanSandboxService(cfg, cli_runner=lambda a, t: (125, b"", b"unreachable"))
    with pytest.raises(SandboxUnavailableError, match="Podman unreachable"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")


def test_failed_native_probe_never_constructs_leaky_sdk_client(monkeypatch):
    """H026: reject failure before the SDK can allocate its orphaned UDS socket."""
    import podman

    created: list[object] = []

    def factory(**kwargs):
        created.append(kwargs)
        raise AssertionError("SDK must not be constructed after failed native probe")

    monkeypatch.setattr(podman, "PodmanClient", factory)
    svc = PodmanSandboxService(
        SandboxConfig(backend="podman", podman_url="unix:///missing.sock"),
        cli_runner=lambda _argv, _timeout: (125, b"", b"unreachable"),
    )
    with pytest.raises(SandboxUnavailableError, match="Podman unreachable"):
        svc._client()
    assert created == []
    assert svc._client_cache is None


def test_native_probe_spawn_failure_is_typed():
    def unavailable(_argv, _timeout):
        raise OSError("podman executable unavailable")

    svc = PodmanSandboxService(
        SandboxConfig(backend="podman", podman_url="unix:///missing.sock"),
        cli_runner=unavailable,
    )
    with pytest.raises(SandboxUnavailableError, match="native client probe could not run"):
        svc._client()


def test_config_is_podman_and_crun_and_cli_url():
    from disco.tools.sandbox import default_podman_config

    cfg = default_podman_config()
    assert cfg.backend == "podman" and cfg.runtime == "crun"
    svc = PodmanSandboxService(cfg)
    assert svc._cli_url == "unix:///run/user/1000/podman/podman.sock"


def test_inspect_state_retries_transient_inspect_failure(monkeypatch):
    """The re-verify inspect RETRIES when the inspect COMMAND itself fails (rc!=0):
    under concurrent load `podman inspect` is intermittently refused, and a single
    failed inspect would conservatively type a LIVE container dead → a needless
    recreate. A retry that then succeeds recovers the real (running) verdict."""
    import disco.tools.sandbox.podman as pod

    monkeypatch.setattr(pod.time, "sleep", lambda _s: None)  # no real backoff in tests
    calls = {"n": 0}

    def flaky(_args, _timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            return (125, b"", b"connection refused")  # the inspect command fails
        return (0, b"status=running OOMKilled=false exit=0 reason=", b"")

    inst = pod.PodmanSandboxInstance.__new__(pod.PodmanSandboxInstance)
    inst._runner, inst._name, inst._cli_url = flaky, "c-flaky", "ssh://x"
    status, _reason = inst._inspect_state()
    assert status == "running", "the retry must recover the live verdict"
    assert calls["n"] == 3  # retried until the inspect succeeded


def test_inspect_state_gives_up_after_retry_budget(monkeypatch):
    """If the inspect keeps failing, it gives up as 'reason-unavailable' after the
    bounded retry budget — never an unbounded loop. A SUCCESSFUL inspect reporting a
    terminal status returns immediately (a real verdict is not retried)."""
    import disco.tools.sandbox.podman as pod

    monkeypatch.setattr(pod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def always_fail(_args, _timeout):
        calls["n"] += 1
        return (125, b"", b"refused")

    inst = pod.PodmanSandboxInstance.__new__(pod.PodmanSandboxInstance)
    inst._runner, inst._name, inst._cli_url = always_fail, "c-dead", "ssh://x"
    status, reason = inst._inspect_state()
    assert status == "" and reason == "reason-unavailable"
    assert calls["n"] == pod._INSPECT_RETRIES  # exactly the budget, no more

    one = {"n": 0}

    def exited(_args, _timeout):
        one["n"] += 1
        return (0, b"status=exited OOMKilled=false exit=1 reason=boom", b"")

    inst2 = pod.PodmanSandboxInstance.__new__(pod.PodmanSandboxInstance)
    inst2._runner, inst2._name, inst2._cli_url = exited, "c-exited", "ssh://x"
    status2, _r2 = inst2._inspect_state()
    assert status2 == "exited" and one["n"] == 1  # a real verdict is NOT retried


# ---------------------------------------------------------------------------
# P-B — FIX6 parity: inbound sidecar preview forwarder + inherited expose_port.
# Mirrors the gVisor FIX6 unit proofs (test_fix6_inbound_forward.py) for podman.
# ---------------------------------------------------------------------------


def _filtered_spec() -> SandboxSpec:
    return SandboxSpec(egress_allow=frozenset({"api.example.com"}))


def _wire_sandbox_ip(client: FakePodmanClient, ip: str) -> None:
    """Make the SANDBOX container report an internal-net IP under the egress net
    (the sidecar — `disco-egr-…` — is created first, so its name is the net name).
    The forwarder reads this to target `host:PORT -> <sandbox_ip>:PORT`."""
    orig = client.create

    def _create(**kwargs):
        c = orig(**kwargs)
        if kwargs.get("name", "").startswith("disco-sbx-"):
            egr = next((x for x in client.created if x.name.startswith("disco-egr-")), None)
            if egr is None:
                return c
            c.attrs = {"NetworkSettings": {"Networks": {egr.name: {"IPAddress": ip}}}}
        return c

    client.create = _create  # type: ignore[method-assign]


async def test_filtered_sidecar_published_with_preview_ports_sandbox_not():
    svc, client, _ = _svc()
    await svc.create(_filtered_spec(), owner_id="o", conversation_id="c")
    # The SIDECAR (disco-egr-…, created first) carries the published preview set —
    # it's the only member on bridge that CAN publish; the sandbox is internal-only.
    sidecar = client.created[0]
    assert sidecar.name.startswith("disco-egr-")
    assert sidecar.create_kwargs.get("ports") == loopback_port_bindings(podman=True)
    assert sidecar.create_kwargs.get("cap_drop") == ["ALL"]
    assert sidecar.create_kwargs.get("no_new_privileges") is True
    # The SANDBOX publishes NOTHING (containment).
    assert "ports" not in client.last.create_kwargs


async def test_podman_egress_proxy_uses_native_detached_exec_without_background_child():
    svc, client, cli = _svc()
    await svc.create(_filtered_spec(), owner_id="o", conversation_id="c")
    sidecar = client.created[0]
    launches = [call for call in cli.calls if "/egress_proxy.py" in " ".join(call)]
    assert len(launches) == 1
    argv = launches[0]
    assert argv[:8] == [
        "podman",
        "--url",
        svc._cli_url,
        "exec",
        "--detach",
        sidecar.name,
        "sh",
        "-c",
    ]
    assert argv.count("--detach") == 1
    shell_command = argv[8]
    assert shell_command.startswith("exec python3 /egress_proxy.py ")
    assert "&" not in shell_command


async def test_podman_sealed_host_service_box_has_capability_only_relay():
    fs: dict[str, bytes] = {}
    client = FakePodmanClient(fs=fs)
    cli = FakeCli(fs)
    cfg = SandboxConfig(
        backend="podman",
        runtime="crun",
        host_service_upstream="https://agent.internal:8443",
    )
    svc = PodmanSandboxService(cfg, client=client, cli_runner=cli)
    inst = await svc.create(SandboxSpec(host_services=True), owner_id="o", conversation_id="c")
    sidecar, sandbox = client.created
    net = client.networks.created[0]
    assert net.attrs["internal"] is True
    assert sidecar.create_kwargs["ports"] == loopback_port_bindings(INTERNAL_PORTS, podman=True)
    assert sandbox.create_kwargs["networks"] == {net.name: {}}
    assert "/capability_relay.py" in fs
    assert inst.host_service_relay_url == "http://10.89.0.2:3211"
    relay_launches = [call for call in cli.calls if "/capability_relay.py" in " ".join(call)]
    assert len(relay_launches) == 1
    relay_argv = relay_launches[0]
    assert relay_argv[:8] == [
        "podman",
        "--url",
        svc._cli_url,
        "exec",
        "--detach",
        sidecar.name,
        "sh",
        "-c",
    ]
    assert relay_argv.count("--detach") == 1
    assert relay_argv[8].startswith("exec python3 /capability_relay.py ")
    assert "&" not in relay_argv[8]
    await inst.destroy()
    assert sidecar.removed and net.removed


async def test_podman_relay_failure_fails_creation_and_cleans_sidecar():
    class BrokenRelayCli(FakeCli):
        def __call__(self, argv, timeout):
            if argv[5:7] == ["python3", "-c"] and argv[-1] == "3211":
                self.calls.append(argv)
                return (1, b"", b"")
            return super().__call__(argv, timeout)

    fs: dict[str, bytes] = {}
    client = FakePodmanClient(fs=fs)
    cli = BrokenRelayCli(fs)
    cfg = SandboxConfig(
        backend="podman",
        runtime="crun",
        host_service_upstream="https://agent.internal:8443",
    )
    svc = PodmanSandboxService(cfg, client=client, cli_runner=cli)
    with pytest.raises(SandboxUnavailableError, match="relay failed readiness"):
        await svc.create(SandboxSpec(host_services=True), owner_id="o", conversation_id="c")
    assert client.created[0].removed
    assert client.networks.created[0].removed
    assert len(client.created) == 1


async def test_inbound_forwarder_uses_native_detached_exec_without_background_child():
    svc, client, cli = _svc()
    _wire_sandbox_ip(client, "10.89.0.7")
    await svc.create(_filtered_spec(), owner_id="o", conversation_id="c")
    assert "/inbound_forward.py" in client.fs
    assert client.fs["/inbound_forward.py"], "the forwarder must be archive-injected"
    # The forwarder is launched on the SIDECAR via native detached CLI exec.
    # Podman's archive API carries the script; detached exec stays short enough
    # for the native remote instead of silently dropping a 7-KiB inline payload.
    launched = [c for c in cli.calls if any("inbound_forward.py" in str(a) for a in c)]
    assert launched, "inbound forwarder was not launched on the sidecar"
    argv = launched[0]
    # Targets the SIDECAR (egress net name), via the proven CLI exec path.
    assert argv[:8] == [
        "podman",
        "--url",
        svc._cli_url,
        "exec",
        "--detach",
        client.created[0].name,
        "sh",
        "-c",
    ]
    assert argv.count("--detach") == 1
    assert argv[5].startswith("disco-egr-")
    shell_command = argv[8]
    assert shell_command.startswith("exec python3 /inbound_forward.py "), shell_command
    assert "base64" not in shell_command
    assert len(shell_command) < 512
    assert "&" not in shell_command
    # targets the SANDBOX's internal IP + every published port.
    assert "10.89.0.7" in shell_command, shell_command
    for port in sorted(PUBLISHED_PORTS):
        assert str(port) in shell_command, f"port {port} missing: {shell_command}"


async def test_no_forwarder_without_sandbox_ip():
    # No internal IP is a typed refusal, not a silently returned sandbox whose
    # published transport can only reset connections. Partial resources are cleaned.
    svc, client, cli = _svc(with_sandbox_ip=False)
    with pytest.raises(SandboxUnavailableError, match="no internal address"):
        await svc.create(_filtered_spec(), owner_id="o", conversation_id="c")
    assert not any("inbound_forward.py" in str(a) for c in cli.calls for a in c)
    assert all(container.removed for container in client.created)
    assert client.networks.created[0].removed
    assert all(volume.removed for volume in client.volumes.objects.values())


async def test_expose_port_inherits_base_impl_no_stub():
    # The podman-only `expose_port` STUB (returned None) is GONE — the class now
    # inherits the shared `_resolve_mapping`-backed impl, same as gVisor.
    from disco.tools.sandbox.podman import PodmanSandboxInstance

    assert "expose_port" not in PodmanSandboxInstance.__dict__


async def test_expose_port_reads_sidecar_binding_for_filtered():
    svc, client, _ = _svc()
    _wire_sandbox_ip(client, "10.89.0.7")
    inst = await svc.create(_filtered_spec(), owner_id="o", conversation_id="c")
    sidecar, sandbox = client.created[0], client.last
    # The SIDECAR holds the real host-published mapping (it published the ports).
    sidecar.attrs = {
        "NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49160"}]}}
    }
    # A decoy binding on the sandbox must be IGNORED for a filtered box.
    sandbox.attrs.setdefault("NetworkSettings", {})["Ports"] = {
        "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1"}]
    }
    url = inst.expose_port(8000)
    assert url == "http://127.0.0.1:49160"  # sidecar's port, not decoy


async def test_sealed_sandbox_maps_only_internal_control_port_from_sidecar():
    svc, client, _ = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    sidecar, sandbox = client.created
    sidecar.attrs = {
        "NetworkSettings": {"Ports": {"8899/tcp": [{"HostIp": "127.0.0.1", "HostPort": "38899"}]}}
    }
    sandbox.attrs.setdefault("NetworkSettings", {})["Ports"] = {
        "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "33333"}],
        "8899/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1"}],
    }
    assert inst._egress_sidecar is sidecar
    assert inst.internal_port_mapping(8899) == ("127.0.0.1", 38899)
    assert inst.expose_port(8000) is None
    assert inst.expose_port(8899) is None


def test_preview_host_derived_from_cli_url():
    from disco.tools.sandbox.podman import _preview_host

    assert _preview_host("ssh://sandbox@203.0.113.47/run/user/1000/podman/podman.sock") == (
        "203.0.113.47"
    )
    assert _preview_host("ssh://user@host.example:22/run/podman.sock") == "host.example"
    assert _preview_host("unix:///run/user/1000/podman/podman.sock") == "localhost"


async def test_instance_preview_host_set_from_cli_url():
    svc, _client, _cli = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert inst._preview_host == "localhost"  # default podman_url is the local rootless socket


# --- runtime honesty: a requested sandbox runtime must actually take effect ----
# Podman's Docker-compat API advertises runsc in /info from a static candidate
# path even when the binary is absent, and drops HostConfig.Runtime on create
# without erroring. A pre-flight check therefore cannot tell the truth here; only
# inspecting what was actually assigned can. These pin that behaviour.


def _runtime_guard(requested: str, effective: str | None):
    """Drive the post-create guard with a container reporting `effective`."""
    from disco.tools.sandbox.podman_parts.container_start import _assert_effective_runtime

    class _Svc:
        _cfg = SimpleNamespace(runtime=requested)

    class _Container:
        def __init__(self) -> None:
            self.attrs: dict = {}
            if effective is not None:
                self.attrs = {"HostConfig": {"Runtime": effective}}

        def reload(self) -> None:
            pass

    return _assert_effective_runtime(_Svc(), _Container())


def test_requested_sandbox_runtime_that_silently_became_the_default_is_refused():
    """The headline case: ask for gVisor, get crun, and be told — not sandboxed."""
    with pytest.raises(SandboxUnavailableError) as excinfo:
        _runtime_guard("runsc", "crun")
    assert "refusing to run unsandboxed" in str(excinfo.value)
    assert "runsc" in str(excinfo.value)


def test_requested_sandbox_runtime_dropped_entirely_is_refused():
    """Podman's compat API drops the field rather than rejecting it."""
    with pytest.raises(SandboxUnavailableError):
        _runtime_guard("runsc", None)


def test_requested_sandbox_runtime_that_took_effect_is_allowed():
    _runtime_guard("runsc", "runsc")  # must not raise


def test_engine_default_runtimes_carry_no_isolation_promise_to_enforce():
    """runc/crun ARE the defaults — asking for one claims nothing to verify."""
    _runtime_guard("crun", None)
    _runtime_guard("runc", None)
    _runtime_guard("", None)


# --- bounds honesty: a requested resource cap must actually take effect --------
# Same "requested is not effective" reasoning as the runtime guard, extended to the
# cgroup bounds. An engine (or compat transport) that DROPS a create field it does
# not support leaves the sandbox unbounded while the caller believes it is capped —
# which makes the bounds validators decorative and allows fork-bomb / memory-
# exhaustion host DoS from agent code.


def _limits_guard(requested: dict[str, int], effective: dict[str, Any] | None):
    """Drive the shared post-create bounds guard against a reported HostConfig."""
    from disco.tools.sandbox._effective_config import assert_effective_limits

    class _Container:
        def __init__(self) -> None:
            self.attrs: dict = {} if effective is None else {"HostConfig": dict(effective)}

        def reload(self) -> None:
            pass

    return assert_effective_limits(_Container(), requested)


def test_requested_bounds_that_took_effect_are_allowed():
    _limits_guard(
        {"Memory": 2048, "CpuQuota": 100_000, "PidsLimit": 512},
        {"Memory": 2048, "CpuQuota": 100_000, "PidsLimit": 512},
    )  # must not raise


def test_a_requested_bound_the_engine_dropped_is_refused():
    with pytest.raises(SandboxUnavailableError) as excinfo:
        _limits_guard({"PidsLimit": 512}, {"PidsLimit": 0})
    assert "requested a pids limit of 512 but the container has none" in str(excinfo.value)


def test_a_requested_bound_missing_from_the_report_entirely_is_refused():
    with pytest.raises(SandboxUnavailableError):
        _limits_guard({"Memory": 2048}, {})


def test_dockers_unlimited_sentinel_is_refused_like_an_absent_bound():
    """Docker spells "no limit" as -1 for PidsLimit; that is not a bound."""
    with pytest.raises(SandboxUnavailableError):
        _limits_guard({"PidsLimit": 512}, {"PidsLimit": -1})


def test_a_bound_the_engine_loosened_is_refused():
    with pytest.raises(SandboxUnavailableError) as excinfo:
        _limits_guard({"Memory": 2048}, {"Memory": 1 << 40})
    assert "was created with" in str(excinfo.value)


def test_a_bound_the_engine_tightened_is_allowed():
    """Stricter than requested means the engine tightened — the host stays protected."""
    _limits_guard({"Memory": 2048, "PidsLimit": 512}, {"Memory": 1024, "PidsLimit": 64})


def test_bounds_guard_fails_closed_when_the_container_cannot_be_inspected():
    from disco.tools.sandbox._effective_config import assert_effective_limits

    class _Container:
        attrs: dict = {}

        def reload(self) -> None:
            raise RuntimeError("daemon closed the connection")

    with pytest.raises(SandboxUnavailableError) as excinfo:
        assert_effective_limits(_Container(), {"PidsLimit": 512})
    assert "could not verify the sandbox resource limits took effect" in str(excinfo.value)


def test_no_requested_bounds_means_nothing_to_verify():
    _limits_guard({}, None)
    _limits_guard({"PidsLimit": 0}, None)  # 0 is the "unset" sentinel, not a claim


async def test_podman_create_refuses_a_bound_the_socket_silently_dropped():
    """End to end through the real Podman create path, not just the helper."""
    svc, client, _cli = _svc()
    real_create = client.create

    def _create(**kwargs):
        container = real_create(**kwargs)
        if str(kwargs.get("name", "")).startswith("disco-sbx-"):
            container.host_config_lies = {"PidsLimit": 0}
        return container

    client.create = _create
    with pytest.raises(SandboxUnavailableError) as excinfo:
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert "refusing to run without the requested resource bounds" in str(excinfo.value)


# --- the cgroup posture HostConfig cannot see ---------------------------------


def test_cgroup_disabling_runtime_args_are_described_for_the_operator():
    """`runsc --runtime-flag ignore-cgroups` records every cap and enforces none, so
    the create call and the post-create inspect both look perfect. The registered
    runtime arguments are the only place the posture is visible."""
    from disco.tools.sandbox._effective_config import cgroup_enforcement_warning

    warning = cgroup_enforcement_warning(
        {"runsc": {"path": "/usr/bin/runsc", "runtimeArgs": ["--ignore-cgroups"]}}, "runsc"
    )
    assert warning is not None
    assert "NOT enforced" in warning


def test_runtime_args_without_a_cgroup_flag_produce_no_warning():
    from disco.tools.sandbox._effective_config import cgroup_enforcement_warning

    assert (
        cgroup_enforcement_warning({"runsc": {"runtimeArgs": ["--platform=systrap"]}}, "runsc")
        is None
    )


def test_absent_runtime_args_are_not_read_as_proof_of_enforcement():
    """Podman's compat /info reports only a path per runtime and no arguments at
    all, so on that transport the signal is simply unavailable — None here means
    "cannot tell", never "verified enforced"."""
    from disco.tools.sandbox._effective_config import cgroup_enforcement_warning

    assert cgroup_enforcement_warning({"runsc": {"path": "/usr/bin/runsc"}}, "runsc") is None
    assert cgroup_enforcement_warning({}, "runsc") is None
