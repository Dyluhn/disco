"""EPIC H §9 — the sandbox ESCAPE SUITE (hermetic).

Proves the four containment properties a sandbox must uphold, against the backend
abstraction (no live container needed; the live proof of the limit FLAGS biting is the
separate bounded-container check run during this epic):

  (a) host-process-SIGNAL refusal — the shared-host `process` backend blanket-refuses
      `kill`/`pkill`/`killall`/`fuser -k`/`… | xargs kill` (a build can't take down the
      agent-server), while a container backend does NOT route through that gate (its
      `kill` only hits its OWN PID namespace).
  (b) host PID namespace — a container is created with its OWN PID namespace
      (`pid_mode` never "host"), so a discovered host PID is unreachable from inside.
  (c) workspace confinement — file ops reject `..`/absolute escapes on BOTH the
      container jail (`_container_path`) and the process jail (`_resolve`).
  (d) network namespace isolation — a sealed box gets `network_mode="none"` (no shared
      host loopback); an open box is on its own bridge netns, never the host's.
"""

from __future__ import annotations

from typing import Any

import pytest
from disco.tools.anatomy import Capability
from disco.tools.sandbox import (
    GvisorSandboxInstance,
    LocalSandboxService,
    SandboxError,
    SandboxSpec,
    default_local_config,
)
from disco.tools.sandbox.process import (
    ProcessSandboxInstance,
    process_backend_signal_command_violation,
)


# ---------------------------------------------------------------------------
# A minimal docker-py stand-in to drive LocalSandboxService.create() and capture
# the create kwargs (netns / pidns / pids). Self-contained — no cross-test import.
# ---------------------------------------------------------------------------
class _FakeContainer:
    def __init__(self, run_kwargs: dict[str, Any]) -> None:
        self.run_kwargs = run_kwargs
        self.attrs: dict[str, Any] = {}

    def reload(self) -> None: ...
    def exec_run(self, *a: Any, **k: Any) -> Any:
        return (0, (b"", b""))


class _FakeImages:
    def get(self, tag: str) -> object:
        return object()


class _FakeVolumes:
    def create(self, name: str) -> None: ...


class _FakeDocker:
    def __init__(self) -> None:
        self.containers = self
        self.images = _FakeImages()
        self.volumes = _FakeVolumes()
        self.last: _FakeContainer | None = None

    def ping(self) -> bool:
        return True

    def info(self) -> dict[str, Any]:
        return {"Runtimes": {"runc": {}, "runsc": {}}}

    def run(self, **kwargs: Any) -> _FakeContainer:
        c = _FakeContainer(kwargs)
        self.last = c
        return c

    def create(self, **kwargs: Any) -> _FakeContainer:  # egress sidecar (unused here)
        return _FakeContainer(kwargs)


def _local_svc(client: Any) -> LocalSandboxService:
    return LocalSandboxService(default_local_config(), client=client)


def _make_container_instance(tmp_ws: str = "/workspace") -> GvisorSandboxInstance:
    """A ContainerInstance whose path-jail can be exercised without a live container —
    `_container_path` rejects escapes BEFORE touching the container object."""
    return GvisorSandboxInstance(
        id="sbx_test",
        owner_id="o",
        conversation_id="c",
        spec=SandboxSpec(),
        container=_FakeContainer({}),
        container_workspace=tmp_ws,
        stop_timeout_s=1,
    )


# ---------------------------------------------------------------------------
# (a) host-process-SIGNAL refusal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cmd",
    [
        "kill 931479",
        "kill -9 1",
        "kill -TERM 42",
        "pkill -f uvicorn",
        "killall python",
        "fuser -k 8000/tcp",
        "lsof -ti:8000 | xargs kill",
        "lsof -ti:8000 | xargs -r kill -9",
        "echo hi && kill 5",  # after a shell separator → still a command head
    ],
)
def test_process_backend_refuses_host_signal_shapes(cmd: str):
    assert process_backend_signal_command_violation(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        "echo 'kill the build'",  # 'kill' inside a quoted argument, not a command head
        "pytest -k kill_switch",  # -k flag, no word boundary
        "ls -la",
        "python -c 'print(1)'",
    ],
)
def test_process_backend_allows_benign_commands(cmd: str):
    assert process_backend_signal_command_violation(cmd) is None


async def test_process_exec_returns_actionable_refusal_for_kill(tmp_path):
    inst = ProcessSandboxInstance("id", "o", "c", SandboxSpec(), tmp_path)
    res = await inst.exec_shell("kill 931479", timeout_s=5)
    assert res.exit_code == 126
    assert res.stderr.startswith("refused:")  # promoted to the model-visible error text
    # the host process was never signalled (no subprocess launched) — refused up front
    assert res.stdout == ""


def test_container_backend_has_no_host_signal_gate():
    # A container backend does NOT route through the process-only signal refusal — its
    # `kill` only reaches its OWN PID namespace, so blanket-refusing would be wrong.
    inst = _make_container_instance()
    assert not hasattr(inst, "_HOST_SIGNAL_PATTERNS")
    # the pure refusal function is scoped to the process backend; it isn't invoked by
    # the container exec path (ContainerInstance.exec_shell has no such call).
    import inspect

    from disco.tools.sandbox import _container

    src = inspect.getsource(_container.ContainerInstance.exec_shell)
    assert "signal_command_violation" not in src


# ---------------------------------------------------------------------------
# (b) host PID namespace — never shared
# ---------------------------------------------------------------------------
async def test_container_create_uses_its_own_pid_namespace():
    client = _FakeDocker()
    svc = _local_svc(client)
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    kw = client.last.run_kwargs
    assert kw.get("pid_mode") != "host"  # NEVER the host PID namespace
    assert "pid_mode" not in kw  # default (private) PID namespace, explicitly unset


# ---------------------------------------------------------------------------
# (c) workspace confinement — escapes rejected on both jails
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "../escape", "a/../../b"])
async def test_container_jail_rejects_path_escape(path: str):
    inst = _make_container_instance()
    with pytest.raises(SandboxError):
        await inst.read_file(path)
    with pytest.raises(SandboxError):
        await inst.write_file(path, b"x")


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "../escape"])
async def test_process_jail_rejects_path_escape(tmp_path, path: str):
    inst = ProcessSandboxInstance("id", "o", "c", SandboxSpec(), tmp_path)
    with pytest.raises(SandboxError):
        await inst.read_file(path)
    with pytest.raises(SandboxError):
        await inst.write_file(path, b"x")


async def test_process_jail_confines_writes_to_workspace(tmp_path):
    # A write that resolves INSIDE the workspace lands there; an escaping one is rejected
    # before any host file is touched (no /etc/passwd write, no parent-dir traversal).
    inst = ProcessSandboxInstance("id", "o", "c", SandboxSpec(), tmp_path)
    await inst.write_file("ok.txt", b"inside")
    assert (tmp_path / "ok.txt").read_bytes() == b"inside"
    with pytest.raises(SandboxError):
        await inst.write_file("../../tmp/escape.txt", b"nope")


# ---------------------------------------------------------------------------
# (d) network namespace isolation
# ---------------------------------------------------------------------------
async def test_sealed_box_has_no_network_namespace_sharing():
    client = _FakeDocker()
    svc = _local_svc(client)
    # default spec → SEALED: no network at all (network_mode="none"), so the box can't
    # reach the host loopback (where the agent-server lives).
    await svc.create(SandboxSpec(), owner_id="o", conversation_id="c")
    assert client.last.run_kwargs.get("network_mode") == "none"


async def test_open_box_is_on_its_own_bridge_not_host_netns():
    client = _FakeDocker()
    svc = _local_svc(client)
    # explicit NETWORK capability → OPEN, but on a bridge netns — NEVER network_mode="host"
    # (which would share the host's loopback + interfaces).
    await svc.create(
        SandboxSpec(permitted=frozenset({Capability.NETWORK})), owner_id="o", conversation_id="c"
    )
    nm = client.last.run_kwargs.get("network_mode")
    assert nm == "bridge"
    assert nm != "host"
