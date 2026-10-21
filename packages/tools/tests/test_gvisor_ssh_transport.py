"""Real pipe + Docker HTTP/exec framing regression, with no daemon or SSH host."""

from __future__ import annotations

import concurrent.futures
import contextlib
import socket
import subprocess
import sys
from typing import Any

import docker
import pytest
from disco.tools.sandbox import GvisorSandboxService, SandboxConfig
from disco.tools.sandbox import gvisor as gvisor_module
from disco.tools.sandbox.gvisor_parts.ssh_transport import (
    _ClosingSSHSocket,
)
from docker.transport import sshconn

# A one-request dial-stdio peer: read the actual SDK request, return real exec
# frames, then close stdin before EOF. This deterministically exercises the
# production race: output consumed, but SDK close writes into an exited peer.
_PEER = """
import os, struct, sys
request = sys.stdin.buffer.readline()
assert request.startswith(b'POST /v1.45/exec/') and b'/start ' in request
length = 0
while True:
    line = sys.stdin.buffer.readline()
    if line == b'\\r\\n': break
    if line.lower().startswith(b'content-length:'): length = int(line.split(b':')[1])
sys.stdin.buffer.read(length)
os.close(0)
sys.stdout.buffer.write(b'HTTP/1.1 101 UPGRADED\\r\\n'
                       b'Connection: Upgrade\\r\\nUpgrade: tcp\\r\\n\\r\\n')
for stream, payload in [(1, b'/workspace/browser-heading.txt\\n'), (2, b'notice\\n')]:
    sys.stdout.buffer.write(struct.pack('>BxxxL', stream, len(payload)) + payload)
sys.stdout.buffer.flush()
"""


@pytest.fixture
def peers(monkeypatch: pytest.MonkeyPatch):
    original_popen = subprocess.Popen
    children: list[Any] = []

    def launch(_args: Any, **_kwargs: Any) -> Any:
        child = original_popen(
            [sys.executable, "-c", _PEER], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        children.append(child)
        return child

    monkeypatch.setattr(sshconn.subprocess, "Popen", launch)
    yield children
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        for pipe in (child.stdin, child.stdout):
            with contextlib.suppress(BrokenPipeError):
                pipe.close()


def test_sdk_cleanup_reproducer_loses_completed_exec_output(peers: list[Any]) -> None:
    """The unmodified dependency demonstrably throws on the same response."""
    client = docker.DockerClient(
        base_url="ssh://fixture", version="1.45", use_ssh_client=True
    )
    try:
        with pytest.raises(BrokenPipeError):
            client.api.exec_start("completed-once", demux=True)
        assert len(peers) == 1
    finally:
        client.close()


def test_concurrent_exec_outputs_survive_peer_exit_without_replay(
    peers: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    constructor = docker.DockerClient
    probes: list[str] = []
    monkeypatch.setattr(gvisor_module, "_probe_ssh_reachable", probes.append)
    monkeypatch.setattr(constructor, "ping", lambda _self: True)
    monkeypatch.setattr(
        docker, "DockerClient", lambda **kwargs: constructor(version="1.45", **kwargs)
    )
    service = GvisorSandboxService(
        SandboxConfig(docker_socket="ssh://fixture", client_timeout_s=7)
    )
    client = service._client()
    assert service._client() is client
    assert probes == ["ssh://fixture"]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(
                pool.map(lambda i: client.api.exec_start(f"completed-{i}", demux=True), range(16))
            )
        assert outputs == [(b"/workspace/browser-heading.txt\n", b"notice\n")] * 16
        assert len(peers) == 16  # Exactly one request/child per exec, never a replay.
        assert all(child.poll() == 0 for child in peers)
        assert all(child.stdin.closed and child.stdout.closed for child in peers)
        adapter = client.api.get_adapter("http+docker://ssh")
        assert adapter.timeout == 7
        assert adapter.max_pool_size == 64
        assert adapter.ssh_client is None  # Still system SSH, with unchanged auth.
    finally:
        client.close()


def test_close_is_idempotent_and_reaps_live_child() -> None:
    sock = _ClosingSSHSocket("fixture")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    sock.proc = child
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: sock.close(), range(8)))
    assert child.poll() is not None
    assert child.stdin is not None and child.stdin.closed
    assert child.stdout is not None and child.stdout.closed
    assert sock.fileno() == -1


def test_send_failure_is_not_swallowed_or_retried() -> None:
    sock = _ClosingSSHSocket("fixture")
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    sock.proc = child
    child.wait(timeout=2)
    try:
        with pytest.raises(BrokenPipeError):
            sock.sendall(b"a real request")
    finally:
        sock.close()
    assert sock.fileno() == -1


def test_unexpected_cleanup_error_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _ClosingSSHSocket("fixture")

    def unexpected(_self: Any) -> None:
        raise PermissionError("unexpected cleanup error")

    monkeypatch.setattr(sshconn.SSHSocket, "close", unexpected)
    with pytest.raises(PermissionError, match="unexpected cleanup"):
        sock.close()
    assert socket.socket.fileno(sock) == -1
