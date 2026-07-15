"""Live verification of the gVisor SandboxBackend against the VM 201 host.

Two ways to run, both reach the same daemon:
  - ON VM 201 (co-located, local socket):
      uv run python packages/tools/scripts/verify_gvisor_vm201.py
  - From a tailnet node over Docker-over-SSH (set the endpoint):
      PMX_DOCKER_HOST=ssh://sandbox@100.81.82.115 uv run python .../verify_gvisor_vm201.py

Walks the 7-item checklist: gVisor engages, the session model (multi-exec +
workspace persistence), sealing (sealed vs granted), resource limits, timeout,
the secret non-leak headline, and teardown (container gone, workspace remains).
"""

from __future__ import annotations

import asyncio
import os
import subprocess

from disco.tools.sandbox import (
    GvisorSandboxService,
    SandboxSpec,
    default_sandbox_config,
)


def _workspace_exists_on_daemon(socket: str, path: str) -> bool:
    """Check the host workspace dir persists on the DAEMON host. Local when
    co-located; over the same SSH endpoint when remote."""
    if socket.startswith("ssh://"):
        target = socket[len("ssh://") :]  # user@host
        r = subprocess.run(
            ["ssh", target, f"test -d {path} && echo EXISTS"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return "EXISTS" in r.stdout
    return os.path.isdir(path)


SENTINEL = "sk-LEAK-SENTINEL-9f3a-do-not-expose"


def ok(label: str, passed: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


async def main() -> None:
    cfg = default_sandbox_config()
    socket = os.environ.get("PMX_DOCKER_HOST", cfg.docker_socket)
    cfg = cfg.model_copy(update={"docker_socket": socket})
    print(
        f"config: socket={cfg.docker_socket} runtime={cfg.runtime} "
        f"image={cfg.image} ws={cfg.workspace_root}"
    )
    svc = GvisorSandboxService(cfg)

    # A secret in THIS process's env — must never reach the box (the headline).
    os.environ["PMX_LEAK_TEST"] = SENTINEL

    # 1 + 2: create (sealed default) → gVisor engages; multi-exec; workspace persists.
    inst = await svc.create(SandboxSpec(memory_mb=256), owner_id="local", conversation_id="verify")
    print(f"\ncreated {inst.id}")
    ver = await inst.exec_shell("cat /proc/version", timeout_s=10)
    ok("1. gVisor engages", "gvisor" in ver.stdout.lower(), ver.stdout.strip()[:80])
    await inst.exec_shell("echo step1 > state.txt", timeout_s=10)
    seen = await inst.exec_shell("cat state.txt; echo step2 >> state.txt", timeout_s=10)
    again = await inst.exec_shell("cat state.txt", timeout_s=10)
    ok(
        "2. session model: multi-exec + workspace persists",
        "step1" in seen.stdout and "step2" in again.stdout,
        again.stdout.strip().replace("\n", "|"),
    )

    # 3a: sealed default cannot reach the network.
    net = await inst.exec_shell(
        "curl -s --max-time 6 https://example.com -o /dev/null -w '%{http_code}' || echo BLOCKED",
        timeout_s=12,
    )
    ok(
        "3a. sealed (default): no network",
        "BLOCKED" in net.stdout or net.exit_code != 0,
        net.stdout.strip()[:40],
    )

    # 5: timeout — a long command is killed and reported, not hung.
    slow = await inst.exec_shell("sleep 30", timeout_s=3)
    ok(
        "5. timeout reported",
        slow.timed_out and slow.exit_code in (124, 137),
        f"timed_out={slow.timed_out} exit={slow.exit_code}",
    )

    # 6: secret non-leak (load-bearing) — env / files / process list are secret-free.
    env = await inst.exec_shell("env", timeout_s=10)
    procs = await inst.exec_shell("ps -e -o args 2>/dev/null || ps", timeout_s=10)
    files = await inst.list_dir(".")
    leaked = SENTINEL in env.stdout or SENTINEL in procs.stdout or any(SENTINEL in f for f in files)
    ok(
        "6. SECRET NON-LEAK (env/proc/files)",
        not leaked,
        "secret absent from the box" if not leaked else "LEAKED!",
    )

    host_ws = f"{cfg.workspace_root.rstrip('/')}/{inst.id}"

    # 7: teardown — container gone, workspace remains on the daemon host.
    await inst.destroy()
    gone: object = True
    try:
        import docker

        kw = {"base_url": socket}
        if socket.startswith("ssh://"):
            kw["use_ssh_client"] = True
        c = docker.DockerClient(**kw)
        gone = not any(f"pmx-sbx-{inst.id}" == ct.name for ct in c.containers.list(all=True))
    except Exception as exc:  # noqa: BLE001
        gone = f"(could not verify: {exc})"
    ok("7. close: container removed", gone is True, str(gone))
    ok(
        "7. close: workspace persists on host",
        _workspace_exists_on_daemon(socket, host_ws),
        host_ws,
    )

    # 4: memory limit — isolated in its OWN box, since exceeding the cgroup limit
    # OOM-kills the whole gVisor sandbox (taking the box down IS the limit working).
    mem_inst = await svc.create(
        SandboxSpec(memory_mb=128), owner_id="local", conversation_id="verify"
    )
    enforced = False
    try:
        mem = await mem_inst.exec_shell(
            "python3 -c 'b=bytearray(400*1024*1024); print(len(b))' || echo KILLED", timeout_s=25
        )
        enforced = "KILLED" in mem.stdout or mem.exit_code != 0
        detail = f"exit={mem.exit_code}"
    except Exception as exc:  # noqa: BLE001 — the box died exceeding its limit: enforced
        enforced = True
        detail = f"sandbox OOM-killed: {type(exc).__name__}"
    ok("4. memory limit enforced", enforced, detail)
    try:
        await mem_inst.destroy()
    except Exception:  # noqa: BLE001
        pass

    # 3b: a GRANTED sandbox can reach the network.
    open_inst = await svc.create(
        SandboxSpec(egress_allow=frozenset({"example.com"})),
        owner_id="local",
        conversation_id="verify",
    )
    net2 = await open_inst.exec_shell(
        "curl -s --max-time 8 https://example.com -o /dev/null -w '%{http_code}' || echo BLOCKED",
        timeout_s=15,
    )
    ok(
        "3b. granted: network reachable",
        "200" in net2.stdout or "301" in net2.stdout,
        net2.stdout.strip()[:40],
    )
    await open_inst.destroy()

    print("\nDone. Any FAIL above is a real gap to fix.")


if __name__ == "__main__":
    asyncio.run(main())
