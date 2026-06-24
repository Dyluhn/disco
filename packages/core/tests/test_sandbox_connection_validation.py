"""P1 #1 — host-less SSH endpoints WITH a port must be rejected as unrunnable.

`_ssh_endpoint_hostless` originally only caught `ssh://sandbox@` (empty after `@`), but
MISSED host-less URLs that still carry a port — `ssh://sandbox@:22`,
`http+ssh://sandbox@:22/run/...` — because `:22` read as a non-empty "host". Those are
the same outage class (no host → "ssh: Could not resolve hostname"), so they must be
rejected. Valid host[:port][/path] endpoints must still pass.
"""

from __future__ import annotations

import pytest
from disco.core.llm import SandboxConnection, default_connection_for, sandbox_connection_error


def _err(backend: str, socket: str) -> str | None:
    return sandbox_connection_error(
        backend, SandboxConnection(docker_socket=socket, runtime="runsc")
    )


@pytest.mark.parametrize(
    "socket",
    [
        "ssh://sandbox@",  # bare (the original outage value)
        "ssh://sandbox@:22",  # host-less WITH a port (the P1 miss)
        "http+ssh://sandbox@:2222/run/docker.sock",  # host-less, port, path
        "ssh://sandbox@/var/run/docker.sock",  # host-less with a path
        "ssh://sandbox@ ",  # whitespace host
        "",  # empty
        "   ",  # blank
    ],
)
def test_gvisor_hostless_sockets_rejected(socket):
    assert _err("gvisor", socket) is not None, f"{socket!r} should be rejected"


@pytest.mark.parametrize(
    "socket",
    [
        "ssh://sandbox@host",
        "ssh://sandbox@host:22",
        "ssh://sandbox@1.2.3.4:22/run/docker.sock",
        "http+ssh://sandbox@100.81.82.115/run/docker.sock",
        "ssh://sandbox@[::1]:22",  # IPv6 literal — has a host
        "unix:///var/run/docker.sock",  # local socket — never host-less
        "tcp://127.0.0.1:2375",  # local TCP — never host-less
    ],
)
def test_valid_sockets_accepted(socket):
    assert _err("gvisor", socket) is None, f"{socket!r} should be accepted"


@pytest.mark.parametrize(
    "url",
    [
        "http+ssh://sandbox@:22/run/podman.sock",  # host-less with port
        "ssh://sandbox@",
        "",
    ],
)
def test_podman_hostless_urls_rejected(url):
    err = sandbox_connection_error("podman", SandboxConnection(podman_url=url))
    assert err is not None, f"{url!r} should be rejected"


def test_process_needs_nothing():
    assert sandbox_connection_error("process", SandboxConnection(docker_socket="")) is None


def test_default_connection_for_is_backend_appropriate_and_never_cross_bleeds():
    """Each backend's clean default is shaped for THAT backend — local/process get the
    local docker socket (never an ssh:// host), gvisor gets the ssh prefill, podman crun."""
    gvisor = default_connection_for("gvisor")
    assert gvisor.docker_socket == "ssh://sandbox@" and gvisor.runtime == "runsc"
    local = default_connection_for("local")
    assert local.docker_socket == "unix:///var/run/docker.sock" and local.runtime == "runc"
    assert "ssh://" not in local.docker_socket  # never a gvisor-shaped socket
    process = default_connection_for("process")
    assert "ssh://" not in process.docker_socket
    podman = default_connection_for("podman")
    assert podman.runtime == "crun" and podman.podman_url  # keeps the rootless remote url
