"""S-W5 exploit regressions: isolation, quotas, and bounded host transfers."""

from __future__ import annotations

import errno
import json
import os
import shlex
import sys
from types import SimpleNamespace

import pytest
from disco.tools.sandbox import (
    GvisorSandboxService,
    LocalSandboxService,
    SandboxConfig,
    SandboxSpec,
    SandboxUnavailableError,
    _container,
)
from disco.tools.sandbox._container import (
    EXEC_CAPTURE_RETURN_BYTES,
    EXEC_CAPTURE_SPILL_BYTES,
    SshLoopbackTunnelManager,
    collect_host_deny_ips,
    discover_local_host_ips,
    discover_remote_host_ips,
    loopback_port_bindings,
)
from disco.tools.sandbox.base import SandboxPermissionError
from disco.tools.sandbox.process import ProcessSandboxService
from test_gvisor import FakeDockerClient
from test_local import FakeLocalClient


async def test_public_web_uses_private_internal_network_and_policy_sidecar(tmp_path):
    client = FakeDockerClient()
    cfg = SandboxConfig(
        workspace_root=str(tmp_path),
        host_ip_blocklist=["93.184.216.34"],
    )
    service = GvisorSandboxService(cfg, client=client)
    instance = await service.create(SandboxSpec(public_web=True), owner_id="o", conversation_id="c")

    sandbox = client.last
    sidecar = client.runs[0]
    assert sandbox.run_kwargs["network"].startswith("disco-egr-")
    assert "network_mode" not in sandbox.run_kwargs
    assert client.networks.created[0].attrs["internal"] is True
    assert sandbox.run_kwargs.get("ports") is None
    assert sidecar.run_kwargs["ports"] == loopback_port_bindings()
    assert sidecar.run_kwargs["cap_drop"] == ["ALL"]
    assert sidecar.run_kwargs["security_opt"] == ["no-new-privileges:true"]
    assert sidecar.run_kwargs["sysctls"] == {
        "net.ipv4.ip_forward": "0",
        "net.ipv6.conf.all.forwarding": "0",
    }
    launch = next(
        call for call in sidecar.exec_calls if any("egress_proxy.py" in str(arg) for arg in call)
    )
    joined = " ".join(launch)
    assert "--public-only" in joined
    assert "--deny-ip" in joined and "93.184.216.34" in joined

    await instance.destroy()
    assert sidecar.removed and client.networks.created[0].removed


async def test_proxy_readiness_failure_cleans_aux_and_refuses_start(tmp_path):
    class FailingReadinessClient(FakeDockerClient):
        def create(self, **kwargs):
            sidecar = super().create(**kwargs)
            # Resolver rewrite succeeds; readiness then fails. Detached launch
            # does not consume an entry in the fake.
            sidecar.exec_results = [(0, b"", b""), (1, b"", b"not listening")]
            return sidecar

    client = FailingReadinessClient()
    service = GvisorSandboxService(SandboxConfig(workspace_root=str(tmp_path)), client=client)
    with pytest.raises(SandboxUnavailableError, match="readiness"):
        await service.create(SandboxSpec(public_web=True), owner_id="o", conversation_id="c")
    assert client.runs[0].removed
    assert client.networks.created[0].removed
    assert client.last is None


async def test_workspace_quota_and_nofile_reach_container_create():
    client = FakeLocalClient()
    cfg = SandboxConfig(
        backend="local",
        runtime="runc",
        default_disk_mb=128,
        default_nofile_soft=256,
        default_nofile_hard=512,
    )
    service = LocalSandboxService(cfg, client=client)
    instance = await service.create(SandboxSpec(disk_mb=64), owner_id="o", conversation_id="c")
    (volume_name,) = client.volumes.created
    volume = client.volumes.objects[volume_name]
    assert volume.options["driver"] == "local"
    assert volume.options["driver_opts"]["type"] == "tmpfs"
    assert "size=64m" in volume.options["driver_opts"]["o"]
    assert client.last.run_kwargs["ulimits"] == [{"Name": "nofile", "Soft": 256, "Hard": 512}]
    await instance.destroy()
    assert volume.removed


async def test_process_shell_huge_output_is_head_tail_bounded_with_capped_spill():
    service = ProcessSandboxService()
    instance = await service.create(SandboxSpec(), owner_id="o", conversation_id="c")
    workspace = instance._workspace
    try:
        code = (
            "import sys; "
            "sys.stdout.write('A' * (3 * 1024 * 1024)); "
            "sys.stderr.write('B' * (2 * 1024 * 1024))"
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        result = await instance.exec_shell(command, timeout_s=20)
        assert result.exit_code == 0
        assert "output truncated at sandbox boundary" in result.stdout
        assert "output truncated at sandbox boundary" in result.stderr
        assert len(result.stdout.encode()) < EXEC_CAPTURE_RETURN_BYTES + 1024
        assert len(result.stderr.encode()) < EXEC_CAPTURE_RETURN_BYTES + 1024
        spills = sorted((workspace / ".disco" / "spills").glob("*.log"))
        assert len(spills) == 2
        assert all(path.stat().st_size == EXEC_CAPTURE_SPILL_BYTES for path in spills)
    finally:
        await instance.destroy()


async def test_small_process_command_does_not_pollute_workspace_with_spill_dirs():
    service = ProcessSandboxService()
    instance = await service.create(SandboxSpec(), owner_id="o", conversation_id="c")
    workspace = instance._workspace
    try:
        result = await instance.exec_shell("printf small", timeout_s=5)
        assert result.stdout == "small"
        assert not (workspace / ".disco").exists()
    finally:
        await instance.destroy()


async def test_process_file_read_rejects_sparse_huge_symlink_and_fifo():
    service = ProcessSandboxService()
    instance = await service.create(SandboxSpec(), owner_id="o", conversation_id="c")
    workspace = instance._workspace
    try:
        huge = workspace / "huge.bin"
        with huge.open("wb") as handle:
            handle.truncate(500 * 1024 * 1024)
        with pytest.raises(OSError) as too_large:
            await instance.read_file("huge.bin")
        assert too_large.value.errno == errno.EFBIG

        (workspace / "zero").symlink_to("/dev/zero")
        with pytest.raises(SandboxPermissionError):
            await instance.read_file("zero")

        os.mkfifo(workspace / "pipe")
        with pytest.raises(SandboxPermissionError):
            await instance.read_file("pipe")
    finally:
        await instance.destroy()


def test_host_address_inventory_includes_endpoint_and_remote_interfaces(monkeypatch):
    payload = [
        {"addr_info": [{"local": "100.64.1.2"}, {"local": "203.0.113.10"}]},
        {"addr_info": [{"local": "fd00::2"}]},
    ]
    monkeypatch.setattr(
        _container.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(payload).encode(), stderr=b""
        ),
    )
    remote = discover_remote_host_ips("ssh://sandbox@100.64.1.2:22")
    denied = collect_host_deny_ips(
        ["198.51.100.7"],
        "100.64.1.2",
        include_local_interfaces=False,
        additional_host_ips=remote,
    )
    assert {"100.64.1.2", "203.0.113.10", "fd00::2", "198.51.100.7"} <= denied


def test_local_host_inventory_includes_every_interface(monkeypatch):
    monkeypatch.setattr(
        _container.psutil,
        "net_if_addrs",
        lambda: {
            "lo": [SimpleNamespace(family=_container.socket.AF_INET, address="127.0.0.1")],
            "lan": [SimpleNamespace(family=_container.socket.AF_INET, address="192.168.1.9")],
            "public": [SimpleNamespace(family=_container.socket.AF_INET, address="8.8.4.4")],
            "tail": [
                SimpleNamespace(family=_container.socket.AF_INET6, address="fd00::9%tailscale0")
            ],
        },
    )
    assert discover_local_host_ips() == frozenset(
        {"127.0.0.1", "192.168.1.9", "8.8.4.4", "fd00::9"}
    )


def test_remote_loopback_mapping_is_forwarded_to_local_loopback(monkeypatch):
    launched = []

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            self.argv = argv
            self.returncode = None
            launched.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(_container.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(SshLoopbackTunnelManager, "_available_port", staticmethod(lambda: 43123))
    monkeypatch.setattr(
        _container.socket, "create_connection", lambda *args, **kwargs: FakeConnection()
    )

    manager = SshLoopbackTunnelManager("ssh://sandbox@100.64.1.2:2222")
    assert manager.forward(49160) == ("127.0.0.1", 43123)
    argv = launched[0].argv
    assert argv[-2:] == ["--", "100.64.1.2"]
    assert argv[argv.index("-L") + 1] == "127.0.0.1:43123:127.0.0.1:49160"
    assert argv.index("-L") < argv.index("--")
    manager.close()
    assert launched[0].returncode == 0
