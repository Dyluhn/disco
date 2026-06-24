"""EPIC H (P2 #5) — the LIVE sandbox-isolation escape suite.

The fast escape-suite tests (`test_escape_suite.py`) assert ARGS/STRINGS (e.g.
`network_mode="none"`, `pid_mode != "host"`) — they prove the backend ASKS for
isolation, not that the kernel DELIVERS it. These tests drive ONE bounded REAL
container and inspect ACTUAL isolation:

  • the real `HostConfig.PidMode` (never "host"),
  • the container's PID + NET namespace INODES differ from the host's,
  • a sealed box cannot reach a service on the HOST's loopback,
  • a sacrificial HOST process survives every signal-bypass form attempted from inside.

MARKER-GATED + SKIPPED BY DEFAULT (`-m sandbox_integration`). It is NEVER in the
default suite / CI — jail safety, and it needs a container runtime + the sandbox image.
RESOURCE DISCIPLINE: each test creates AT MOST ONE container, bounded
(`--memory=2g --cpus=2 --pids-limit=512`), and removes it immediately (`--rm` + a
`finally` force-remove). Run it once, locally, to confirm real enforcement.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator

import pytest

_RUNTIME = shutil.which("podman") or shutil.which("docker")
_IMAGE = "localhost/disco-sandbox:base"


def _image_present() -> bool:
    if _RUNTIME is None:
        return False
    for tag in (_IMAGE, "disco-sandbox:base"):
        r = subprocess.run(  # noqa: S603
            [_RUNTIME, "image", "exists", tag], capture_output=True
        )
        if r.returncode == 0:
            return True
    return False


pytestmark = [
    pytest.mark.sandbox_integration,
    pytest.mark.skipif(
        _RUNTIME is None or not _image_present(),
        reason="needs a container runtime + the disco-sandbox image (run -m sandbox_integration)",
    ),
]


def _run(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    assert _RUNTIME is not None
    return subprocess.run(  # noqa: S603
        [_RUNTIME, *args], capture_output=True, text=True, timeout=timeout
    )


@contextlib.contextmanager
def _bounded_box(network: str = "none") -> Iterator[str]:
    """Create ONE bounded container, yield its name, force-remove it on exit. At most
    one container is alive per `with` block (the tests never nest these)."""
    name = f"disco-sbx-itest-{uuid.uuid4().hex[:12]}"
    res = _run(
        "run", "-d", "--rm", "--name", name,
        "--memory=2g", "--cpus=2", "--pids-limit=512",
        f"--network={network}",
        _IMAGE, "sleep", "infinity",
    )
    assert res.returncode == 0, f"failed to start bounded box: {res.stderr}"
    try:
        yield name
    finally:
        _run("rm", "-f", name, timeout=30)


def _exec(name: str, *cmd: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return _run("exec", name, *cmd, timeout=timeout)


# ---------------------------------------------------------------------------
# (b) the REAL PID namespace — HostConfig.PidMode + actual inode comparison
# ---------------------------------------------------------------------------
def test_real_hostconfig_pidmode_is_never_host():
    with _bounded_box() as name:
        res = _run("inspect", name)
        assert res.returncode == 0
        info = json.loads(res.stdout)[0]
        pid_mode = info["HostConfig"].get("PidMode", "")
        assert pid_mode != "host", f"container joined the HOST pid namespace: {pid_mode!r}"


def test_pid_and_net_namespace_inodes_differ_from_host():
    host_pid = subprocess.run(  # noqa: S603
        ["readlink", "/proc/self/ns/pid"], capture_output=True, text=True
    ).stdout.strip()
    host_net = subprocess.run(  # noqa: S603
        ["readlink", "/proc/self/ns/net"], capture_output=True, text=True
    ).stdout.strip()
    with _bounded_box() as name:
        box_pid = _exec(name, "readlink", "/proc/self/ns/pid").stdout.strip()
        box_net = _exec(name, "readlink", "/proc/self/ns/net").stdout.strip()
    assert box_pid and box_pid != host_pid, f"PID ns not isolated: {box_pid} == {host_pid}"
    assert box_net and box_net != host_net, f"NET ns not isolated: {box_net} == {host_net}"


# ---------------------------------------------------------------------------
# (d) network namespace — a sealed box cannot reach a HOST loopback service
# ---------------------------------------------------------------------------
def test_sealed_box_cannot_reach_host_loopback():
    # Bring up a real host listener on 127.0.0.1:<ephemeral>.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _accept() -> None:
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except OSError:
                continue

    t = threading.Thread(target=_accept, daemon=True)
    t.start()
    try:
        with _bounded_box(network="none") as name:
            # From inside the sealed box, 127.0.0.1 is the container's OWN loopback, not
            # the host's — the connect must fail (the host listener is unreachable).
            probe = (
                "import socket,sys\n"
                f"s=socket.socket(); s.settimeout(2)\n"
                "try:\n"
                f"    s.connect(('127.0.0.1',{port})); print('REACHED'); sys.exit(0)\n"
                "except OSError as e:\n"
                "    print('ISOLATED', e); sys.exit(7)\n"
            )
            res = _exec(name, "python3", "-c", probe)
            assert "REACHED" not in res.stdout, "sealed box reached the HOST loopback service!"
            assert res.returncode != 0
    finally:
        stop.set()
        srv.close()


# ---------------------------------------------------------------------------
# (a) host PID signalling — a sacrificial HOST process survives every bypass form
# ---------------------------------------------------------------------------
def test_host_process_survives_every_signal_bypass_form():
    # A real sacrificial HOST process the box must NOT be able to kill.
    victim = subprocess.Popen(  # noqa: S603
        ["sleep", "600"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        hostpid = victim.pid
        with _bounded_box() as name:
            # Multiple bypass forms — all must fail because the host PID is simply not
            # present in the container's OWN pid namespace (real kernel containment, not
            # the best-effort process-backend string scan).
            forms = [
                ("shell kill -TERM", ["sh", "-c", f"kill -TERM {hostpid}"]),
                ("shell kill -9", ["sh", "-c", f"kill -9 {hostpid}"]),
                ("python os.kill", ["python3", "-c", f"import os;os.kill({hostpid},15)"]),
                ("kill -0 probe", ["sh", "-c", f"kill -0 {hostpid}"]),
            ]
            for label, cmd in forms:
                res = _exec(name, *cmd)
                assert res.returncode != 0, f"{label}: signal unexpectedly SUCCEEDED on host pid"
            # The victim is still alive a moment later (no signal landed).
            time.sleep(0.5)
            assert victim.poll() is None, "the sacrificial HOST process was killed from the box!"
    finally:
        with contextlib.suppress(Exception):
            victim.terminate()
            victim.wait(timeout=5)


# ---------------------------------------------------------------------------
# (c) workspace confinement — a guest symlink out of /workspace is refused LIVE
# ---------------------------------------------------------------------------
def test_guest_symlink_escape_refused_live(tmp_path):
    # Drive the REAL LocalSandboxInstance against a live container: create a symlink in
    # the guest pointing at /etc, then prove the file API refuses to read through it.
    import asyncio

    from disco.tools.sandbox import LocalSandboxService, SandboxError, SandboxSpec
    from disco.tools.sandbox.config import default_local_config

    async def _drive() -> None:
        cfg = default_local_config()
        # point docker-py at the local podman socket if that's the runtime
        if _RUNTIME and _RUNTIME.endswith("podman"):
            cfg.docker_socket = "unix:///run/user/1000/podman/podman.sock"
            cfg.runtime = "crun"
        cfg.image = _IMAGE
        svc = LocalSandboxService(cfg)
        inst = await svc.create(SandboxSpec(), owner_id="o", conversation_id="c-itest")
        try:
            # Create the malicious symlink inside the guest workspace.
            await inst.exec_shell("ln -s /etc /workspace/out", timeout_s=20)
            # Reading through it must be refused (resolves to /etc, outside the jail).
            with pytest.raises(SandboxError):
                await inst.read_file("out/passwd")
        finally:
            await inst.destroy()

    asyncio.run(_drive())
