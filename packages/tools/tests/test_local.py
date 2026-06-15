"""Local container backend — hermetic (mocked local Docker/Podman socket).

Proves the local tier is the proven Docker backend on a LOCAL socket with `runc`: the
create/exec/close session, sealing, limits-in-create, never-pull, timeout, typed errors,
no env leak, and the per-run NAMED VOLUME workspace (the one behavioral difference from
gVisor). Plus the #5 contract: the isolation profile is honest about being weaker AND
its coupled confirmation default is STRICTLY tighter than the strong tier's. The real
limits-bite / secret-non-leak is the live local-socket check.
"""

from __future__ import annotations

import io
import tarfile
from collections import namedtuple

import pytest
from disco.core import SecurityRisk
from disco.tools.anatomy import Capability
from disco.tools.sandbox import (
    LocalSandboxService,
    SandboxConfig,
    SandboxError,
    SandboxSpec,
    SandboxUnavailableError,
    default_local_config,
    isolation_for,
)
from docker.errors import ImageNotFound

_Exec = namedtuple("_Exec", ["exit_code", "output"])


class FakeContainer:
    def __init__(self, run_kwargs: dict) -> None:
        self.run_kwargs = run_kwargs
        self.stopped = self.removed = self.started = False
        self.exec_calls: list[list[str]] = []
        self.exec_results: list[tuple[int, bytes, bytes]] = []
        self.fs: dict[str, bytes] = {}
        self.attrs: dict = {}  # NetworkSettings populated on reload() if needed (E8)

    def exec_run(self, cmd, demux=False, workdir=None, detach=False):
        self.exec_calls.append(cmd)
        if detach:
            return _Exec(0, (b"", b"") if demux else b"")
        if cmd[0] == "cat":
            path = cmd[-1]
            code, out, err = (0, self.fs[path], b"") if path in self.fs else (1, b"", b"no file")
        elif cmd[0] == "ls":
            prefix = cmd[-1].rstrip("/") + "/"
            names = sorted({p[len(prefix):].split("/")[0] for p in self.fs if p.startswith(prefix)})
            code, out, err = 0, ("\n".join(names) + "\n").encode() if names else b"", b""
        elif cmd[0] == "mkdir":
            code, out, err = 0, b"", b""
        elif self.exec_results:
            code, out, err = self.exec_results.pop(0)
        else:
            code, out, err = 0, b"ok\n", b""
        return _Exec(code, (out, err) if demux else (out or b"") + (err or b""))

    def put_archive(self, path, data):
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for m in tar.getmembers():
                f = tar.extractfile(m)
                self.fs[path.rstrip("/") + "/" + m.name] = f.read() if f else b""
        return True

    def start(self):
        self.started = True

    def reload(self):  # docker-py refreshes .attrs; the fake leaves them empty
        pass

    def stop(self, timeout=None):
        self.stopped = True

    def remove(self, force=False):
        self.removed = True


class _Images:
    def __init__(self, has: bool) -> None:
        self._has = has

    def get(self, tag):
        if not self._has:
            raise ImageNotFound(f"no such image: {tag}")
        return object()


class _Volumes:
    def __init__(self) -> None:
        self.created: list[str] = []

    def create(self, name):
        self.created.append(name)


class _FakeLocalNetwork:
    """Mirrors docker-py Network on a local-socket client. `connect()` joins a
    container, `remove()` tears the network down. The parent gVisor fake has the
    same shape; mirrored here so the local-tier tests can drive the same E8
    filtered-egress wiring (the local backend reuses the parent's
    `_setup_filtered_egress`)."""

    def __init__(self, name: str, **attrs) -> None:
        self.name = name
        self.attrs = attrs
        self.connected: list = []
        self.removed = False

    def connect(self, container, aliases=None):
        self.connected.append((container, aliases))

    def remove(self):
        self.removed = True


class _FakeLocalNetworks:
    def __init__(self) -> None:
        self.created: list[_FakeLocalNetwork] = []

    def create(self, name, **kwargs):
        net = _FakeLocalNetwork(name, **kwargs)
        self.created.append(net)
        return net


class FakeLocalClient:
    """A LOCAL docker-py-style client (no SSH) — Docker or a Podman Docker-compat socket."""

    def __init__(self, *, runtimes=("runc", "crun"), has_image: bool = True) -> None:
        self._runtimes = {r: {} for r in runtimes}
        self.images = _Images(has_image)
        self.volumes = _Volumes()
        self.networks = _FakeLocalNetworks()
        self.containers = self
        self.last: FakeContainer | None = None
        self.runs: list[FakeContainer] = []  # every container started (sidecar + sandbox, E8)

    def ping(self):
        return True

    def info(self):
        return {"Runtimes": self._runtimes}

    def run(self, **kwargs):
        c = FakeContainer(kwargs)
        self.runs.append(c)
        self.last = c
        return c

    def create(self, **kwargs):  # the egress sidecar (E8)
        c = FakeContainer(kwargs)
        self.runs.append(c)
        return c  # NOT self.last: the sandbox (via run) stays the asserted-on container


def _svc(client: FakeLocalClient) -> LocalSandboxService:
    return LocalSandboxService(default_local_config(), client=client)


async def test_create_exec_close_named_volume_on_local_socket():
    client = FakeLocalClient()
    svc = _svc(client)
    inst = await svc.create(SandboxSpec(), owner_id="local", conversation_id="c")
    kw = client.last.run_kwargs
    assert kw["command"] == ["sleep", "infinity"]
    assert kw["runtime"] == "runc"  # the local-tier runtime, from config
    # workspace is a per-run NAMED VOLUME bound to /workspace (not a host bind path)
    (vol,) = client.volumes.created
    assert kw["volumes"][vol] == {"bind": "/workspace", "mode": "rw"}
    assert vol.startswith("disco-ws-")
    # session: multiple execs into the SAME container, timeout-wrapped
    r1 = await inst.exec_shell("echo hi", timeout_s=10)
    await inst.exec_shell("echo bye", timeout_s=10)
    assert r1.exit_code == 0 and r1.stdout == "ok\n" and r1.timed_out is False
    assert len(client.last.exec_calls) == 2
    assert client.last.exec_calls[0][:1] == ["timeout"]
    await inst.destroy()
    assert client.last.stopped and client.last.removed
    with pytest.raises(SandboxError):
        await inst.exec_shell("echo", timeout_s=5)


async def test_never_pulls_when_image_absent():
    client = FakeLocalClient(has_image=False)
    svc = _svc(client)
    with pytest.raises(SandboxUnavailableError, match="never pulls"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last is None and client.volumes.created == []  # nothing created


async def test_sealed_default_open_when_granted():
    client = FakeLocalClient()
    svc = _svc(client)
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last.run_kwargs["network_mode"] == "none"
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})), owner_id="o", conversation_id="c"
    )
    assert client.last.run_kwargs["network_mode"] == "bridge"


async def test_limits_and_no_env_leak_in_create(monkeypatch):
    monkeypatch.setenv("PMX_FAKE_SECRET", "sk-do-not-leak")
    client = FakeLocalClient()
    svc = _svc(client)
    await svc.create(SandboxSpec(cpu=2.0, memory_mb=512), owner_id="o", conversation_id="c")
    kw = client.last.run_kwargs
    assert kw["mem_limit"] == "512m"  # the limit goes through the local socket/daemon
    assert kw["nano_cpus"] == 2_000_000_000
    assert kw["environment"] == {}  # no host env into the box


async def test_timeout_reported_and_file_round_trip():
    client = FakeLocalClient()
    svc = _svc(client)
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    await inst.write_file("sub/a.txt", b"hello")
    assert await inst.read_file("sub/a.txt") == b"hello"
    assert "sub" in await inst.list_dir(".")
    with pytest.raises(SandboxError):
        await inst.read_file("../../etc/passwd")
    client.last.exec_results = [(124, b"partial\n", b"")]
    res = await inst.exec_shell("sleep 999", timeout_s=1)
    assert res.timed_out is True and res.exit_code == 124 and res.stdout == "partial\n"


async def test_runtime_missing_is_a_typed_error():
    client = FakeLocalClient(runtimes=("crun",))  # runc not registered
    svc = _svc(client)
    with pytest.raises(SandboxUnavailableError, match="runc"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")


async def test_local_unreachable_is_a_typed_error():
    # No injected client → real docker-py against a bogus LOCAL socket → typed error.
    cfg = SandboxConfig(backend="local", runtime="runc", docker_socket="unix:///nonexistent.sock")
    svc = LocalSandboxService(cfg)
    with pytest.raises(SandboxUnavailableError, match="Docker unreachable"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")


def test_config_is_local_and_runc_and_local_socket():
    cfg = default_local_config()
    assert cfg.backend == "local" and cfg.runtime == "runc"
    # default connection is the LOCAL docker socket — no SSH, no remote url
    assert cfg.docker_socket.startswith("unix://") and "ssh" not in cfg.docker_socket
    assert LocalSandboxService(cfg).name == "local"


# ---- #5: lower-isolation labeling + tighter-confirmation coupling -------------


def test_local_isolation_is_labeled_weaker_and_not_adversarial_safe():
    local = isolation_for("local")
    gvisor = isolation_for("gvisor")
    assert local.adversarial_safe is False and gvisor.adversarial_safe is True
    # the label is honest about shared-kernel / weaker-than-gVisor at the point of choice
    assert "shared host kernel" in local.label.lower()
    assert "not adversarial" in local.label.lower()
    # the service carries its own profile
    assert LocalSandboxService().isolation.backend == "local"


def test_confirmation_coupling_local_strictly_tighter_than_gvisor():
    local = isolation_for("local").recommended_confirmation()
    gvisor = isolation_for("gvisor").recommended_confirmation()
    # A MEDIUM-risk action: the weak local tier gates it; the strong gVisor tier does not.
    assert local.should_confirm(SecurityRisk.MEDIUM) is True
    assert gvisor.should_confirm(SecurityRisk.MEDIUM) is False
    # UNKNOWN risk: local gates (fail-safe); gVisor trusts the agent.
    assert local.should_confirm(SecurityRisk.UNKNOWN) is True
    assert gvisor.should_confirm(SecurityRisk.UNKNOWN) is False
    # both still gate HIGH — tighter never means looser anywhere.
    assert local.should_confirm(SecurityRisk.HIGH) and gvisor.should_confirm(SecurityRisk.HIGH)


def test_unknown_backend_falls_back_to_weakest():
    prof = isolation_for("mystery-backend")
    assert prof.adversarial_safe is False
    # weakest fallback gates even LOW risk
    assert prof.recommended_confirmation().should_confirm(SecurityRisk.LOW) is True
