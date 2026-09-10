# ruff: noqa: E501 — a verification script; long task/log lines are inherent.
"""LIVE proof of life for the plan-and-approve stage on the Build surface.

  uv run python current/packages/agent-server/scripts/verify_plan_approve.py

End-to-end:
  1. Submit a task. The loop starts in PLANNING and pauses at AWAITING_PLAN_APPROVAL.
  2. Print the proposed plan; approve it; assert the build runs (real tools).
  3. The per-action ConfirmRisky gate STILL bites mid-build (defense in depth);
     auto-approve those gated actions (a human would decide).
  4. After FINISHED, request a re-plan with a focused change → assert a NEW plan
     proposal (revision 2) lands and we halt again for approval.
  5. Approve the revised plan and let it run.

Needs the AGENT_DRIVER model reachable (default_config → the local Qwen llama-server)
and a local container sandbox (rootless Podman socket + disco-sandbox:base).
"""

from __future__ import annotations

import asyncio

from disco.agent_server import ConversationRuntime
from disco.core import (
    ActionEvent,
    AgentErrorEvent,
    CondensationEvent,
    ConversationState,
    ConversationStatus,
    ErrorEvent,
    EventSource,
    LLMMessage,
    MessageEvent,
    ObservationEvent,
    PlanEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.env import disco_env
from disco.core.llm.config import default_config
from disco.tools.sandbox import LocalSandboxService, SandboxConfig, SandboxSpec

CID = "plan-approve"
TASK = (
    "You are working in a sandbox at /workspace. The goal: write a Python program "
    "fib.py that prints the first 10 Fibonacci numbers (one per line), then run it "
    "in the shell and report the output."
)
REPLAN = (
    "Add a small unit test for the fibonacci logic without touching fib.py itself — "
    "put it in test_fib.py and run it."
)


def _get(d, k):
    return d.get(k) if isinstance(d, dict) else None


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


def _print_new(events: list, shown: int) -> int:
    for e in events[shown:]:
        if isinstance(e, MessageEvent):
            who = e.source.value
            print(f"  · {who}: {e.message.content.strip()[:140]}")
        elif isinstance(e, PlanEvent):
            print(f"  ★ PLAN (rev {e.revision}): {e.summary.strip()[:120]}")
            for i, s in enumerate(e.steps, start=1):
                print(f"      {i}. {s.title}")
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
            print(f"  [FATAL ERROR] {str(getattr(e, 'detail', e))[:300]}")
        elif isinstance(e, CondensationEvent):
            print(
                f"  ~ CONDENSED seq {e.forgotten_start_seq}-{e.forgotten_end_seq}: {e.summary[:80]}"
            )
        elif isinstance(e, StatusEvent):
            print(f"  [status] {e.status.value}{(' · ' + e.detail) if e.detail else ''}")
    return len(events)


_READ_TOOLS = {"file_read", "file_list", "search", "extract"}


def _round_stats(events: list) -> tuple[int, int]:
    """Return (actions_count, planning_reads_count) computed from the trace so far.
    planning_reads counts read actions that happened BEFORE a plan was proposed
    (the exploration phase Claude Code calls "Phase 1: Initial Understanding")."""
    actions_count = sum(
        1 for e in events if isinstance(e, ActionEvent) and e.tool_call is not None
    )
    first_plan_seq = next(
        (e.seq for e in events if isinstance(e, PlanEvent)),
        None,
    )
    planning_reads = sum(
        1
        for e in events
        if isinstance(e, ActionEvent)
        and e.tool_call is not None
        and e.tool_call.tool_name in _READ_TOOLS
        and first_plan_seq is not None
        and (e.seq or 0) < first_plan_seq
    )
    return actions_count, planning_reads


async def _handle_plan_gate(runtime: ConversationRuntime, *, auto_approve_plan: bool) -> bool:
    """Handle AWAITING_PLAN_APPROVAL. Returns True to keep driving (continue), False
    to hand control back to the caller (break)."""
    if auto_approve_plan:
        print("  [PLAN GATE] human would review here → auto-APPROVING")
        await runtime.approve_plan(CID)
        return True
    print("  [PLAN GATE] returning to caller for handling")
    return False


async def _handle_action_gate(
    runtime: ConversationRuntime, events: list, state: ConversationState
) -> None:
    """WAITING_FOR_CONFIRMATION: auto-approve the risky action mid-build (a human
    would decide here) — defense in depth on top of the plan gate."""
    pending = next(
        (
            e
            for e in events
            if isinstance(e, ActionEvent) and e.id == state.pending_action_id
        ),
        None,
    )
    risk = _get(pending.meta.get("risk_assessment") if pending else None, "risk")
    print(f"  [ACTION GATE] risk={risk} — auto-APPROVING (human would decide here)")
    await runtime.confirm(CID)


async def _drive(
    runtime: ConversationRuntime, store: SqliteEventStore, *, auto_approve_plan: bool, shown: int
) -> tuple[int, dict]:
    """Drive the loop to a terminal-for-now status, printing events as they stream.
    Auto-approve the plan gate ONCE (the first time it appears), auto-approve risky
    actions, and return counters about what happened. Returns (shown, stats)."""
    stats = {"actions": 0, "plan_gates": 0, "action_gates": 0, "planning_reads": 0}
    for _round in range(60):
        runtime.run_controller.kick(CID)
        task = runtime.run_registry.task(CID)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=240)
            except TimeoutError:
                print("  [TIMEOUT waiting for a step]")
                break
        events = await store.get_events(CID)
        shown = _print_new(events, shown)
        stats["actions"], stats["planning_reads"] = _round_stats(events)
        state = await store.get_state(CID)

        if state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL:
            stats["plan_gates"] += 1
            if await _handle_plan_gate(runtime, auto_approve_plan=auto_approve_plan):
                continue
            break
        if state.execution_status == ConversationStatus.WAITING_FOR_CONFIRMATION:
            stats["action_gates"] += 1
            await _handle_action_gate(runtime, events, state)
            continue
        if state.execution_status in (
            ConversationStatus.FINISHED,
            ConversationStatus.ERROR,
            ConversationStatus.IDLE,
            ConversationStatus.STUCK,
        ):
            break
    return shown, stats


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
    runtime.settings._set_surface(CID, "build")
    await store.append(CID, _user(TASK))
    print(f"\nTASK: {TASK[:120]} …\n--- agent trace (round 1: first plan + build) ---")

    # Round 1: drive until either (a) the plan gate fires (we approve, then carry on
    # to the per-action gate or to FINISHED) or (b) something terminal happens.
    shown, s1 = await _drive(runtime, store, auto_approve_plan=True, shown=0)
    state = await store.get_state(CID)

    plan_events = [e for e in await store.get_events(CID) if isinstance(e, PlanEvent)]
    first_plan_ctx_len = len(plan_events[0].context) if plan_events else 0
    print("\n--- round 1 result ---")
    print(f"  status         : {state.execution_status.value}")
    print(
        f"  plan gates     : {s1['plan_gates']} (PLANNING → AWAITING_PLAN_APPROVAL → approve → build)"
    )
    print(
        f"  planning reads : {s1['planning_reads']} (Phase 1 exploration — file_list/file_read/search/extract)"
    )
    print(f"  plan ctx chars : {first_plan_ctx_len} (the markdown rationale on the PlanEvent)")
    print(
        f"  action gates   : {s1['action_gates']} (per-action ConfirmRisky still bites mid-build)"
    )
    print(f"  tool actions   : {s1['actions']}")

    ok1 = (
        state.execution_status == ConversationStatus.FINISHED
        and s1["plan_gates"] >= 1
        and s1["actions"] >= 2
    )
    print(
        f"  [{'PASS' if ok1 else 'CHECK'}] plan-first: model proposed, we approved, build ran to FINISHED"
    )

    # Round 2: re-enter plan mode with a focused diff request, expect a NEW plan
    # (revision 2) at AWAITING_PLAN_APPROVAL — the re-plan affordance the UI exposes.
    print(f"\n--- round 2: re-enter plan mode → '{REPLAN[:70]}…' ---")
    await runtime.request_plan(CID, REPLAN)
    shown, s2 = await _drive(runtime, store, auto_approve_plan=True, shown=shown)
    final = await store.get_state(CID)
    events = await store.get_events(CID)
    plans = [e for e in events if isinstance(e, PlanEvent)]

    print("\n--- final result ---")
    print(f"  status       : {final.execution_status.value}")
    print(f"  plans seen   : {len(plans)} (revisions: {[p.revision for p in plans]})")
    print(f"  plan gates   : {s2['plan_gates']} this round")
    print(f"  actions tot  : {s2['actions']}")

    ok2 = (
        final.execution_status == ConversationStatus.FINISHED
        and len(plans) >= 2
        and plans[-1].revision >= 2
        and s2["plan_gates"] >= 1
    )
    print(
        f"  [{'PASS' if ok2 else 'CHECK'}] re-plan: a new plan (revision {plans[-1].revision if plans else '?'}) "
        f"was proposed for the change, approved, and ran to {final.execution_status.value}"
    )


if __name__ == "__main__":
    asyncio.run(main())
