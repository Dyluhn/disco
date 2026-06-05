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
from docker.errors import ImageNotFound
from perpleximanus.tools.anatomy import Capability
from perpleximanus.tools.sandbox import (
    GvisorSandboxService,
    SandboxConfig,
    SandboxError,
    SandboxSpec,
    SandboxUnavailableError,
)

_Exec = namedtuple("_Exec", ["exit_code", "output"])


class FakeContainer:
    def __init__(self, run_kwargs: dict) -> None:
        self.run_kwargs = run_kwargs
        self.stopped = False
        self.removed = False
        self.exec_calls: list[list[str]] = []
        # queued (exit_code, stdout_bytes, stderr_bytes); default echoes success
        self.exec_results: list[tuple[int, bytes, bytes]] = []

    def exec_run(self, cmd, demux=False, workdir=None):
        self.exec_calls.append(cmd)
        if self.exec_results:
            code, out, err = self.exec_results.pop(0)
        else:
            code, out, err = 0, b"ok\n", b""
        return _Exec(code, (out, err) if demux else (out or b"") + (err or b""))

    def stop(self, timeout=None):
        self.stopped = True

    def remove(self, force=False):
        self.removed = True


class FakeDockerClient:
    def __init__(self, *, runtimes=("runsc", "runc"), run_error: Exception | None = None) -> None:
        self._runtimes = {r: {} for r in runtimes}
        self._run_error = run_error
        self.containers = self
        self.last: FakeContainer | None = None

    def ping(self):
        return True

    def info(self):
        return {"Runtimes": self._runtimes}

    def run(self, **kwargs):  # client.containers.run(**kwargs)
        if self._run_error is not None:
            raise self._run_error
        self.last = FakeContainer(kwargs)
        return self.last


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


async def test_sealed_by_default_open_when_granted(tmp_path):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    # default spec → sealed
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last.run_kwargs["network_mode"] == "none"
    # egress grant → open
    await svc.create(
        SandboxSpec(egress_allow=frozenset({"api.example.com"})), owner_id="o", conversation_id="c"
    )
    assert client.last.run_kwargs["network_mode"] == "bridge"
    # NETWORK capability also counts as a raw-egress grant
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})), owner_id="o", conversation_id="c"
    )
    assert client.last.run_kwargs["network_mode"] == "bridge"


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


async def test_file_round_trip_and_escape_rejection_via_bind_mount(tmp_path):
    client = FakeDockerClient()
    svc = _svc(tmp_path, client)
    inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    await inst.write_file("sub/a.txt", b"hello")
    assert await inst.read_file("sub/a.txt") == b"hello"
    assert "sub" in await inst.list_dir(".")
    # workspace jail: escapes are rejected
    with pytest.raises(SandboxError):
        await inst.read_file("../../etc/passwd")
    with pytest.raises(SandboxError):
        await inst.write_file("/etc/evil", b"x")


async def test_runsc_missing_is_a_typed_error(tmp_path):
    client = FakeDockerClient(runtimes=("runc",))  # gVisor not configured
    svc = _svc(tmp_path, client)
    with pytest.raises(SandboxUnavailableError, match="runsc"):
        await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")


async def test_image_missing_is_a_typed_error(tmp_path):
    client = FakeDockerClient(run_error=ImageNotFound("no such image"))
    svc = _svc(tmp_path, client)
    with pytest.raises(SandboxUnavailableError, match="image"):
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
