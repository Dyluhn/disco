"""gVisor sandbox backend — hermetic (mocked Docker SDK), tool-sandbox §5/§7.

Proves the create/exec/close session model, capability-set network sealing,
resource limits, timeout reporting, the typed infra-error paths, and the headline
secret non-leak (the backend passes NO host env to the container) — all offline.
The real isolation (gVisor engages, sealing actually blocks the net) is the live
VM-201 check; here we prove the backend drives Docker correctly.
"""

from __future__ import annotations

from collections import namedtuple
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
from docker.errors import ImageNotFound

_Exec = namedtuple("_Exec", ["exit_code", "output"])


class FakeContainer:
    def __init__(self, run_kwargs: dict) -> None:
        self.run_kwargs = run_kwargs
        self.stopped = False
        self.removed = False
        self.started = False
        self.attrs: dict = {}  # NetworkSettings populated on reload() if needed
        self.exec_calls: list[list[str]] = []
        # queued (exit_code, stdout_bytes, stderr_bytes) for shell execs; else echoes ok
        self.exec_results: list[tuple[int, bytes, bytes]] = []
        self.fs: dict[str, bytes] = {}  # in-container files, by absolute path
        self.tar_mtimes: dict[str, int] = {}  # mtime of each put_archive'd member

    def start(self):
        self.started = True

    def reload(self):  # docker-py refreshes .attrs; the fake leaves them empty
        pass

    def exec_run(self, cmd, demux=False, workdir=None, detach=False):
        self.exec_calls.append(cmd)
        if detach:
            return _Exec(0, (b"", b"") if demux else b"")
        # file ops the backend issues: cat / ls / mkdir against the tiny FS
        if cmd[0] == "cat":
            path = cmd[-1]
            if path in self.fs:
                code, out, err = 0, self.fs[path], b""
            else:
                code, out, err = 1, b"", b"cat: No such file"
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
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for m in tar.getmembers():
                f = tar.extractfile(m)
                self.fs[path.rstrip("/") + "/" + m.name] = f.read() if f else b""
                self.tar_mtimes[path.rstrip("/") + "/" + m.name] = m.mtime
        return True

    def stop(self, timeout=None):
        self.stopped = True

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


class FakeNetwork:
    """Stands in for a docker-py Network (filtered-egress internal network)."""

    def __init__(self, name: str, **attrs: Any) -> None:
        self.name = name
        self.attrs = attrs
        self.connected: list[Any] = []  # containers connected, with aliases
        self.removed = False

    def connect(self, container, aliases=None):
        self.connected.append((container, aliases))

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
        self.runs.append(c)
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
    assert kw["image"] == "pmx-sandbox:base"
    # multiple execs into the SAME container (session, not one-shot)
    r1 = await inst.exec_shell("echo hi", timeout_s=10)
    await inst.exec_shell("echo bye", timeout_s=10)
    assert r1.exit_code == 0 and r1.stdout == "ok\n" and r1.timed_out is False
    assert len(client.last.exec_calls) == 2  # same container, two execs
    # the command is wrapped in the container-side timeout
    assert client.last.exec_calls[0][:1] == ["timeout"]
    assert "echo hi" in client.last.exec_calls[0]
    # close stops + removes the container
    await inst.destroy()
    assert client.last.stopped and client.last.removed
    # use-after-destroy is rejected
    with pytest.raises(SandboxError):
        await inst.exec_shell("echo", timeout_s=5)


async def test_sealed_by_default_filtered_on_allowlist_open_on_capability(tmp_path):
    # The THREE-way egress posture (the fix for the old none|bridge binary that
    # silently gave an allowlisted box full network).
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    # 1) default spec → SEALED (no network at all)
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last.run_kwargs["network_mode"] == "none"
    # 2) egress allowlist → FILTERED (proxy enforces the list) — NOT raw bridge.
    await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})), owner_id="o", conversation_id="c"
    )
    sandbox_kw = client.last.run_kwargs
    assert "network_mode" not in sandbox_kw  # NOT full bridge!
    assert sandbox_kw["network"].startswith("pmx-egr-")  # the internal no-NAT net
    assert sandbox_kw["environment"]["HTTPS_PROXY"].startswith("http://pmx-egr-")
    # 3) raw NETWORK capability, no allowlist → OPEN (deliberate raw egress).
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})), owner_id="o", conversation_id="c"
    )
    assert client.last.run_kwargs["network_mode"] == "bridge"


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
    joined = " ".join(launched[0])
    assert "api.example.com" in joined and ".pypi.org" in joined
    # destroy() tears down BOTH the sandbox and the egress aux (no orphans).
    await inst.destroy()
    assert sidecar.removed and net.removed


async def test_resource_limits_and_workspace_mount_applied(tmp_path):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    await svc.create(SandboxSpec(cpu=2.0, memory_mb=512), owner_id="o", conversation_id="c")
    kw = client.last.run_kwargs
    assert kw["mem_limit"] == "512m"
    assert kw["nano_cpus"] == 2_000_000_000
    # workspace bind-mounted rw to the container's /workspace
    (host_bind,) = kw["volumes"].keys()
    assert kw["volumes"][host_bind] == {"bind": "/workspace", "mode": "rw"}


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
    # No injected client → it builds a real client against a bogus socket and fails.
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
