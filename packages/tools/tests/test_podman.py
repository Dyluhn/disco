"""Podman sandbox backend — hermetic (mocked podman-py client + CLI runner).

Proves the session model, sealing, limits-in-create, NEVER-PULL, timeout reporting,
typed-error paths, file round-trip (put_archive write + CLI cat/ls read), and no
env leak — all offline. The real isolation + limits-bite-through-the-socket is live.
"""

from __future__ import annotations

import io
import tarfile

import pytest
from disco.tools.anatomy import Capability
from disco.tools.sandbox import (
    PodmanSandboxService,
    SandboxConfig,
    SandboxError,
    SandboxSpec,
    SandboxUnavailableError,
)


class FakeContainer:
    def __init__(self, create_kwargs: dict, fs: dict[str, bytes]) -> None:
        self.create_kwargs = create_kwargs
        self.fs = fs  # shared with the CLI runner (write here, read there)
        self.started = self.stopped = self.removed = False
        self.attrs: dict = {}  # populated by reload() — E8 sidecar needs Networks
        # a queue of fake (exit_code, stdout, stderr) for exec_run; consumed FIFO.
        self.exec_results: list[tuple[int, bytes, bytes]] = []
        self.exec_calls: list[list[str]] = []
        # podman-py Container.name — the sidecar setup reads it to build the
        # `podman --url … exec <name> …` CLI argv. The fake's `name` follows
        # the create-kwarg convention (matches the real podman-py .name).
        self.name = create_kwargs.get("name", "")

    def start(self):
        self.started = True

    def reload(self):  # no-op: the fake leaves attrs alone (callers set NetworkSettings)
        pass

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
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for m in tar.getmembers():
                f = tar.extractfile(m)
                self.fs[path.rstrip("/") + "/" + m.name] = f.read() if f else b""
        return True

    def stop(self, timeout=None):
        self.stopped = True

    def remove(self, force=False):
        self.removed = True


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

    def create(self, name):
        self.created.append(name)


class FakePodmanNetwork:
    """Mirrors podman-py Network: connect() joins a container, remove() tears it down."""

    def __init__(self, name: str, **attrs) -> None:
        self.name = name
        self.attrs = attrs
        self.connected: list = []
        self.removed = False

    def connect(self, container, aliases=None):
        self.connected.append((container, aliases))

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
    def __init__(self, *, has_image: bool = True, fs: dict | None = None) -> None:
        self.images = _Images(has_image)
        self.volumes = _Volumes()
        self.networks = _FakeNetworks()
        self.containers = self
        self.fs = fs if fs is not None else {}
        self.last: FakeContainer | None = None
        self.created: list[FakeContainer] = []  # every container create()'d (sidecar + sandbox)

    def ping(self):
        return True

    def create(self, **kwargs):
        c = FakeContainer(kwargs, self.fs)
        self.created.append(c)
        self.last = c
        return c


class FakeCli:
    """Stands in for `podman --url … exec …`. cat/ls/mkdir against the shared FS;
    queued results for shell (timeout-wrapped) commands."""

    def __init__(self, fs: dict[str, bytes]) -> None:
        self.fs = fs
        self.results: list[tuple[int, bytes, bytes]] = []
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        rest = argv[5:]  # after ["podman","--url",url,"exec",name]
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
        if self.results:
            return self.results.pop(0)
        return (0, b"ok\n", b"")


def _svc(has_image: bool = True) -> tuple[PodmanSandboxService, FakePodmanClient, FakeCli]:
    fs: dict[str, bytes] = {}
    client = FakePodmanClient(has_image=has_image, fs=fs)
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
    assert "timeout" in cli.calls[-1]  # in-container timeout wrap
    await inst.destroy()
    assert client.last.stopped and client.last.removed


async def test_never_pulls_when_image_absent():
    svc, client, _cli = _svc(has_image=False)
    with pytest.raises(SandboxUnavailableError, match="never pulls"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.images.pull_called is False and client.last is None


async def test_sealed_default_open_when_granted():
    svc, client, _ = _svc()
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last.create_kwargs["network_mode"] == "none"
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})), owner_id="o", conversation_id="c"
    )
    assert client.last.create_kwargs["network_mode"] == "bridge"


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
    # 1) NOT the old fail-safe seal.
    assert "network_mode" not in client.last.create_kwargs
    # 2) the sandbox IS on the internal no-NAT net (proxied route out).
    networks = client.last.create_kwargs.get("networks") or {}
    assert networks and next(iter(networks)).startswith("disco-egr-")
    # 3) the allowlist proxy env is present (defense in depth atop the no-route net).
    env = client.last.create_kwargs["environment"]
    assert env["HTTPS_PROXY"].startswith("http://") and "8888" in env["HTTPS_PROXY"]


async def test_limits_and_no_env_leak_in_create(monkeypatch):
    monkeypatch.setenv("PMX_FAKE_SECRET", "sk-do-not-leak")
    svc, client, _ = _svc()
    await svc.create(SandboxSpec(cpu=2.0, memory_mb=512), owner_id="o", conversation_id="c")
    kw = client.last.create_kwargs
    assert kw["mem_limit"] == "512m"  # the limit goes through the socket create
    assert kw["cpu_quota"] == 200_000 and kw["cpu_period"] == 100_000
    assert kw["environment"] == {}  # no host env into the box


async def test_timeout_reported_and_file_round_trip():
    svc, _client, cli = _svc()
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    # write (put_archive) -> read/list (CLI), jailed
    await inst.write_file("sub/a.txt", b"hello")
    assert await inst.read_file("sub/a.txt") == b"hello"
    assert "sub" in await inst.list_dir(".")
    with pytest.raises(SandboxError):
        await inst.read_file("../../etc/passwd")
    # timeout → reported (CLI returns 124), partial output preserved
    cli.results = [(124, b"partial\n", b"")]
    res = await inst.exec_shell("sleep 999", timeout_s=1)
    assert res.timed_out is True and res.exit_code == 124 and res.stdout == "partial\n"


async def test_podman_unreachable_is_typed_error():
    # bogus UNIX socket → fails fast (an ssh:// url would block on the connect),
    # exercising the same _client() error-mapping path.
    cfg = SandboxConfig(backend="podman", podman_url="http+unix:///nonexistent/podman.sock")
    svc = PodmanSandboxService(cfg, cli_runner=lambda a, t: (0, b"", b""))
    with pytest.raises(SandboxUnavailableError, match="Podman unreachable"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")


def test_config_is_podman_and_crun_and_cli_url():
    from disco.tools.sandbox import default_podman_config

    cfg = default_podman_config()
    assert cfg.backend == "podman" and cfg.runtime == "crun"
    svc = PodmanSandboxService(cfg)
    assert svc._cli_url.startswith("ssh://") and not svc._cli_url.startswith("http+")
