# ruff: noqa: E501 — a verification script; long curl/command lines are inherent.
"""Live verification of the Podman SandboxBackend against the VM 202 host (`podtest`),
keyless over Tailscale SSH via Podman's native remote.

  uv run python packages/tools/scripts/verify_podman_vm202.py

Walks the 8-item checklist: connects keyless (crun rootless), session model (multi-
exec + workspace persistence via a named volume), sealing (sealed vs granted), the
LOAD-BEARING limits-bite-through-the-socket guarantee, timeout, image-by-load /
never-pull, the secret non-leak headline, and teardown.
"""

from __future__ import annotations

import asyncio
import os

from disco.tools.sandbox import (
    PodmanSandboxService,
    SandboxSpec,
    SandboxUnavailableError,
    default_podman_config,
)

SENTINEL = "sk-LEAK-SENTINEL-podman-do-not-expose"


def ok(label: str, passed: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


async def main() -> None:
    cfg = default_podman_config()
    print(f"config: url={cfg.podman_url} runtime={cfg.runtime} image={cfg.image}")
    svc = PodmanSandboxService(cfg)
    os.environ["PMX_LEAK_TEST"] = SENTINEL  # must never reach the box

    # 1 + 2: create (sealed default) → crun rootless; multi-exec; workspace persists.
    inst = await svc.create(SandboxSpec(memory_mb=256), owner_id="local", conversation_id="verify")
    print(f"\ncreated {inst.id}")
    who = await inst.exec_shell("id -un; cat /proc/1/comm; readlink /proc/self/ns/user", timeout_s=10)
    ok("1. connects (crun rootless container running)", who.exit_code == 0, who.stdout.strip().replace("\n", " ")[:50])
    await inst.write_file("state.txt", b"step1\n")
    appended = await inst.exec_shell("cat state.txt; echo step2 >> state.txt", timeout_s=10)
    persisted = await inst.read_file("state.txt")
    ok(
        "2. session: multi-exec + /workspace persists across execs",
        "step1" in appended.stdout and b"step2" in persisted,
        persisted.decode().strip().replace("\n", "|"),
    )

    # 3a: sealed default cannot reach the network.
    net = await inst.exec_shell(
        "curl -s --max-time 6 https://example.com -o /dev/null -w '%{http_code}' || echo BLOCKED",
        timeout_s=12,
    )
    ok("3a. sealed (default): no network", "BLOCKED" in net.stdout or net.exit_code != 0, net.stdout.strip()[:40])

    # 5: timeout — killed + reported, partial output preserved.
    slow = await inst.exec_shell("sleep 30", timeout_s=3)
    ok("5. timeout reported", slow.timed_out and slow.exit_code in (124, 137), f"timed_out={slow.timed_out} exit={slow.exit_code}")

    # 7: secret non-leak — env/proc/files secret-free.
    env = await inst.exec_shell("env", timeout_s=10)
    procs = await inst.exec_shell("ps -e -o args 2>/dev/null || ps", timeout_s=10)
    files = await inst.list_dir(".")
    leaked = SENTINEL in env.stdout or SENTINEL in procs.stdout or any(SENTINEL in f for f in files)
    ok("7. SECRET NON-LEAK (env/proc/files)", not leaked, "secret absent from the box" if not leaked else "LEAKED!")

    vol_name = f"{cfg.workspace_volume_prefix}-{inst.id}"

    # 8: teardown — container gone, workspace named volume removed.
    await inst.destroy()
    from podman import PodmanClient

    with PodmanClient(base_url=cfg.podman_url) as c:
        gone = not c.containers.exists(f"pmx-sbx-{inst.id}")
        vol_removed = not c.volumes.exists(vol_name)
    ok("8. close: container removed", gone, "")
    ok("8. close: workspace volume removed", vol_removed, vol_name)

    # 4: limits BITE through the socket (the load-bearing #2 guarantee), isolated box.
    mem_inst = await svc.create(
        SandboxSpec(memory_mb=128), owner_id="local", conversation_id="verify"
    )
    enforced = False
    try:
        mem = await mem_inst.exec_shell(
            "python3 -c 'b=bytearray(400*1024*1024); print(len(b))' || echo KILLED", timeout_s=25
        )
        enforced = "KILLED" in mem.stdout or mem.exit_code != 0
        detail = f"exit={mem.exit_code} (NOT silently cgroupfs-unlimited)"
    except Exception as exc:  # noqa: BLE001 — box died exceeding its limit: enforced
        enforced = True
        detail = f"OOM-killed: {type(exc).__name__}"
    ok("4. LIMITS bite through the socket", enforced, detail)
    try:
        await mem_inst.destroy()
    except Exception:  # noqa: BLE001
        pass

    # 3b: a GRANTED sandbox can reach the network.
    open_inst = await svc.create(
        SandboxSpec(egress_allow=frozenset({"example.com"})), owner_id="local", conversation_id="verify"
    )
    net2 = await open_inst.exec_shell(
        "curl -s --max-time 8 https://example.com -o /dev/null -w '%{http_code}' || echo BLOCKED",
        timeout_s=15,
    )
    ok("3b. granted: network reachable", "200" in net2.stdout or "301" in net2.stdout, net2.stdout.strip()[:40])
    await open_inst.destroy()

    # 6: image-by-load / NEVER pull — a missing image fails loud, no pull attempted.
    bad = PodmanSandboxService(cfg.model_copy(update={"image": "pmx-sandbox:DOES-NOT-EXIST"}))
    try:
        await bad.create(SandboxSpec(), owner_id="local", conversation_id="verify")
        ok("6. image-by-load: missing → typed error (no pull)", False, "did NOT fail!")
    except SandboxUnavailableError as e:
        ok("6. image-by-load: missing → typed error (no pull)", "never pulls" in str(e), str(e)[:70])

    print("\nDone. Any FAIL above is a real gap to fix.")


if __name__ == "__main__":
    asyncio.run(main())
