# ruff: noqa: E501 — a verification script; long command lines are inherent.
"""Live verification of the core agent tools + the resilient session, driven through
the real executor against a LOCAL container sandbox (no SSH). Run on a host with a
local Docker or rootless Podman socket:

  uv run python packages/tools/scripts/verify_agent_tools_local.py

Defaults to this host's rootless Podman socket; override via the same env the local
backend uses (PMX_LOCAL_SOCKET / PMX_LOCAL_RUNTIME / PMX_LOCAL_IMAGE).

Proves, with a real box behind real tools:
  1. exec genuinely captures real output + true exit code (lesson #2 — a pass is only
     trustworthy if the mechanism did the work).
  2. file round-trip + jail (write/read/list through the tools; escape rejected).
  3. timeout → timed_out surfaced, partial output preserved.
  4. containment holds (lesson #3): sealed (no network) by default; a host-env secret
     is absent from the box.
  5. THE HEADLINE (lesson #1): a sandbox dying mid-session (OOM kills the whole box) is
     caught by the session, re-created, surfaced as a clean sandbox_error ToolResult —
     and the next tool call works on the fresh box. Never wedges.
"""

from __future__ import annotations

import asyncio
import os

from disco.core import ToolCall
from disco.tools.builtin import build_default_registry
from disco.tools.executor import DefaultToolExecutor
from disco.tools.registry import agent_scope
from disco.tools.sandbox import LocalSandboxService, SandboxSession, SandboxSpec

SENTINEL = "sk-LEAK-SENTINEL-agent-tools-do-not-expose"


def ok(label: str, passed: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def _service() -> LocalSandboxService:
    from disco.tools.sandbox import SandboxConfig

    cfg = SandboxConfig(
        backend="local",
        runtime=os.environ.get("PMX_LOCAL_RUNTIME", "runc"),
        docker_socket=os.environ.get("PMX_LOCAL_SOCKET", "unix:///run/user/1000/podman/podman.sock"),
        image=os.environ.get("PMX_LOCAL_IMAGE", "pmx-sandbox:base"),
    )
    return LocalSandboxService(cfg)


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(tool_name=name, arguments=arguments)


async def main() -> None:
    os.environ["PMX_LEAK_TEST"] = SENTINEL  # a secret in the agent-server env
    svc = _service()
    registry = build_default_registry()

    # ---- a normal session: tools drive a real box --------------------------------
    session = SandboxSession(svc, SandboxSpec(memory_mb=256), conversation_id="verify")
    ex = DefaultToolExecutor(registry, agent_scope(), sandbox=session, default_timeout_s=20)

    # 1. exec genuinely captures (real output + a non-trivial exit code)
    r = await ex.execute(_call("shell", command="echo CAPTURED_LIVE; exit 7"))
    captured = r.structured and r.structured.get("stdout", "").strip() == "CAPTURED_LIVE" and r.structured.get("exit_code") == 7
    ok("1. exec genuinely captures real output + exit code", bool(captured), f"stdout={r.structured.get('stdout','').strip()!r} exit={r.structured.get('exit_code')}")

    # 2. file round-trip through the tools + jail
    await ex.execute(_call("file_write", path="notes/todo.txt", content="step1\n"))
    rd = await ex.execute(_call("file_read", path="notes/todo.txt"))
    ls = await ex.execute(_call("file_list", path="notes"))
    esc = await ex.execute(_call("file_read", path="../../etc/passwd"))
    ok(
        "2. file round-trip via tools + workspace jail",
        rd.success and rd.content.strip() == "step1" and "todo.txt" in ls.structured["entries"] and esc.success is False,
        f"read={rd.content.strip()!r} jail_rejected={not esc.success}",
    )

    # 3. timeout → timed_out surfaced, partial output preserved
    t = await ex.execute(_call("shell", command="echo partial; sleep 30"))
    ok(
        "3. timeout surfaced (timed_out, partial output)",
        t.success is False and t.structured.get("timed_out") is True and "partial" in t.content,
        f"timed_out={t.structured.get('timed_out')} exit={t.structured.get('exit_code')}",
    )

    # 4a. containment: sealed by default (no network)
    net = await ex.execute(_call("shell", command="curl -s --max-time 6 https://example.com -o /dev/null -w '%{http_code}' || echo BLOCKED"))
    ok("4a. sealed by default (no network)", "BLOCKED" in net.content or not net.success, net.content.strip()[:30])

    # 4b. containment: the host-env secret is absent from the box (and exec really ran)
    env = await ex.execute(_call("shell", command="env"))
    leaked = SENTINEL in (env.structured.get("stdout", "") or "")
    ok("4b. secret non-leak (host env absent in box)", (not leaked) and len(env.structured.get("stdout", "")) > 0, "secret absent; env captured" if not leaked else "LEAKED!")

    await session.destroy()

    # ---- THE HEADLINE: sandbox dies mid-session → re-create (lesson #1) -----------
    death_session = SandboxSession(svc, SandboxSpec(memory_mb=256), conversation_id="death")
    dx = DefaultToolExecutor(registry, agent_scope(), sandbox=death_session, default_timeout_s=20)

    warm = await dx.execute(_call("shell", command="echo alive; cat /etc/hostname"))
    gen_before = death_session.generation

    # Kill the WHOLE box out from under the session. NOTE on why this is an *external*
    # stop and not an in-box OOM/`kill 1`: a cgroup OOM kills the offending PROCESS, not
    # pid 1, and the kernel IGNORES SIGKILL to pid 1 from within its own namespace — so
    # the box survives both. A crash that takes the whole box (the VM 202 finding) is
    # faithfully simulated by stopping the container the session is holding.
    live = death_session._instance  # the live ContainerInstance
    assert live is not None
    await asyncio.to_thread(live._container.stop, timeout=1)  # type: ignore[attr-defined]

    # the next tool call lands on the dead box: the session catches the typed death,
    # re-creates, and the executor returns a clean sandbox_error — never raises/wedges.
    died = await dx.execute(_call("shell", command="echo SHOULD_NOT_RUN_ON_DEAD_BOX"))
    recreated = death_session.generation > gen_before

    # once re-created, a fresh call succeeds on the new box.
    fresh = await dx.execute(_call("shell", command="echo FRESH_BOX_OK"))
    healed = fresh.success and "FRESH_BOX_OK" in fresh.content

    ok("5. warm-up call ran on the box", warm.success and "alive" in warm.content, "")
    ok(
        "5a. death surfaced as a clean sandbox_error ToolResult (no raise)",
        died.success is False and (died.structured or {}).get("kind") == "sandbox_error",
        f"kind={(died.structured or {}).get('kind')}; content={died.content[:40]!r}",
    )
    ok(
        "5b. HEADLINE: session re-created the box → next call healthy (no wedge)",
        recreated and healed,
        f"generation {gen_before}→{death_session.generation}; fresh box healthy={healed}",
    )
    await death_session.destroy()

    print("\nDone. Any FAIL above is a real gap to fix.")


if __name__ == "__main__":
    asyncio.run(main())
