# ruff: noqa: E501 — a verification script; long task/log lines are inherent.
"""LIVE proof of life for the Agent (Build) surface — a REAL model, REAL agent tools,
REAL container sandbox, driving the composed loop end to end.

  uv run python packages/agent-server/scripts/verify_build_proof_of_life.py

Needs the AGENT_DRIVER model reachable (default_config → the local Qwen llama-server)
and a local container sandbox (this host's rootless Podman socket + disco-sandbox:base).

Runs a multi-step coding task: write a program, run it in the sandbox, observe the
output, then a DESTRUCTIVE cleanup (rm -rf) that the ConfirmRisky gate should pause on.
The script auto-approves any gated action (logging the risk), so we see the gate BITE
*and* confirm execute exactly it — and the agent run to FINISHED. Plan→act→observe is
printed as it streams.
"""

from __future__ import annotations

import asyncio

from disco.agent_server import ConversationRuntime
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    ConversationStatus,
    ErrorEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.env import disco_env
from disco.core.llm.config import default_config
from disco.tools.sandbox import LocalSandboxService, SandboxConfig, SandboxSpec

CID = "proof-of-life"
TASK = (
    "You are working in a sandbox at /workspace. Do these steps, using your tools:\n"
    "1. Write a file fib.py that prints the first 10 Fibonacci numbers, one per line.\n"
    "2. Run it with the shell and look at the output.\n"
    "3. Clean up by deleting the file with `rm -rf fib.py`.\n"
    "Then briefly tell me the output you saw and confirm cleanup. Keep it short."
)


def _print_new(events: list, shown: int) -> int:
    for e in events[shown:]:
        if isinstance(e, MessageEvent):
            who = e.source.value
            print(f"  · {who}: {e.message.content.strip()[:140]}")
        elif isinstance(e, ActionEvent):
            tc = e.tool_call
            args = tc.arguments if tc else {}
            print(f"  → ACTION {tc.tool_name if tc else '(finish)'} {str(args)[:120]}")
            if e.thought:
                print(f"      thought: {e.thought.strip()[:120]}")
        elif isinstance(e, ObservationEvent):
            r = e.tool_result
            print(
                f"      ✓ obs (exit {(_get(r.structured, 'exit_code'))}): {r.content.strip()[:120]!r}"
            )
        elif isinstance(e, AgentErrorEvent):
            print(f"      ✗ {e.error[:120]}")
        elif isinstance(e, ErrorEvent):
            print(f"  [FATAL ERROR] {str(getattr(e, 'error', e))[:300]}")
        elif isinstance(e, CondensationEvent):
            print(
                f"  ~ CONDENSED seq {e.forgotten_start_seq}-{e.forgotten_end_seq}: {e.summary[:80]}"
            )
        elif isinstance(e, StatusEvent):
            print(f"  [status] {e.status.value}{(' · ' + e.detail) if e.detail else ''}")
    return len(events)


def _get(d, k):
    return d.get(k) if isinstance(d, dict) else None


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


async def main() -> None:
    cfg = default_config()
    print("driver = default_config AGENT_DRIVER (local Qwen); sandbox = local container (runc)")

    sbx = LocalSandboxService(
        SandboxConfig(
            backend="local",
            runtime=disco_env("LOCAL_RUNTIME", "runc"),
            docker_socket=disco_env("LOCAL_SOCKET", "unix:///run/user/1000/podman/podman.sock"),
            image=disco_env("SANDBOX_IMAGE", "disco-sandbox:base"),
        )
    )
    store = SqliteEventStore(":memory:")
    runtime = ConversationRuntime(
        store, config=cfg, sandbox_service=sbx, sandbox_spec=SandboxSpec(memory_mb=512)
    )
    store.create_conversation(CID, owner_id="local")
    runtime.set_surface(CID, "build")
    await store.append(CID, _user(TASK))
    print(f"\nTASK: {TASK.splitlines()[0]} …\n--- agent trace ---")

    shown = 0
    gates_seen = 0
    actions = 0
    for _round in range(30):
        runtime.kick(CID)
        task = runtime._run_registry.task(CID)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=240)
            except TimeoutError:
                print("  [TIMEOUT waiting for a step]")
                break
        events = await store.get_events(CID)
        shown = _print_new(events, shown)
        actions = sum(1 for e in events if isinstance(e, ActionEvent) and e.tool_call is not None)
        state = await store.get_state(CID)

        if state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION:
            pending = next(
                (
                    e
                    for e in events
                    if isinstance(e, ActionEvent) and e.id == state.pending_action_id
                ),
                None,
            )
            risk = _get(pending.meta.get("risk_assessment") if pending else None, "risk")
            gates_seen += 1
            print(f"  [GATE] action paused, risk={risk} — auto-APPROVING (human would decide here)")
            await runtime.confirm(CID)
            continue
        if state.execution_status in (
            ConversationStatus.FINISHED,
            ConversationStatus.ERROR,
            ConversationStatus.IDLE,
            ConversationStatus.STUCK,
        ):
            break

    final = await store.get_state(CID)
    print("\n--- result ---")
    print(f"  final status : {final.execution_status.value}")
    print(f"  tool actions : {actions}")
    print(
        f"  gates fired  : {gates_seen} (the ConfirmRisky gate paused execution, then confirm ran it)"
    )
    ok = final.execution_status == ConversationStatus.FINISHED and actions >= 2
    print(
        f"\n  [{'PASS' if ok else 'CHECK'}] proof of life: real model + real tools + real sandbox drove a multi-step task to {final.execution_status.value}"
    )


if __name__ == "__main__":
    asyncio.run(main())
