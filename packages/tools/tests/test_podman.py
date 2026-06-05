"""Podman sandbox backend — hermetic (mocked podman-py client + CLI runner).

Proves the session model, sealing, limits-in-create, NEVER-PULL, timeout reporting,
typed-error paths, file round-trip (put_archive write + CLI cat/ls read), and no
env leak — all offline. The real isolation + limits-bite-through-the-socket is live.
"""

from __future__ import annotations

import io
import tarfile

import pytest
from perpleximanus.tools.anatomy import Capability
from perpleximanus.tools.sandbox import (
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

    def start(self):
        self.started = True

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


class FakePodmanClient:
    def __init__(self, *, has_image: bool = True, fs: dict | None = None) -> None:
        self.images = _Images(has_image)
        self.volumes = _Volumes()
        self.containers = self
        self.fs = fs if fs is not None else {}
        self.last: FakeContainer | None = None

    def ping(self):
        return True

    def create(self, **kwargs):
        self.last = FakeContainer(kwargs, self.fs)
        return self.last


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
    assert cli.calls[-1][:5] == ["podman", "--url", svc._cli_url, "exec", f"pmx-sbx-{inst.id}"]
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
    from perpleximanus.tools.sandbox import default_podman_config

    cfg = default_podman_config()
    assert cfg.backend == "podman" and cfg.runtime == "crun"
    svc = PodmanSandboxService(cfg)
    assert svc._cli_url.startswith("ssh://") and not svc._cli_url.startswith("http+")
