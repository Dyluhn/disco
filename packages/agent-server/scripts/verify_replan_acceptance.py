# ruff: noqa: E501 — a verification script; long task/log lines are inherent.
"""LIVE acceptance for the tailscale-runthru fix set (B2/B4/B5/B6).

  source ~/.config/disco/agent.env   # DISCO_OPENROUTER_API_KEY
  uv run python packages/agent-server/scripts/verify_replan_acceptance.py

Drives a REAL model (disco-config.json → gpt-oss-120b) through a REAL local
container sandbox: a small two-file build to FINISHED, then a NEW instruction
that must RE-PLAN before executing. Asserts, at the event level:

  B4 — no FALSE "file doesn't exist / done-condition NOT met" C18 advisory fires
       for files the agent actually wrote (the container-backend bug).
  B5 — the completed build lands FINISHED, not PAUSED/actionless.
  B2 + B6 — the new instruction RE-ENTERS planning: a PlanEvent with revision>=2
       is emitted, and it appears BEFORE any execution write in the re-plan phase
       (i.e. the agent plans first instead of free-building → no infinite spinner).
"""

from __future__ import annotations

import asyncio

from disco.agent_server import ConversationRuntime
from disco.core import (
    ActionEvent,
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    PlanEvent,
    SqliteEventStore,
    StatusEvent,
)
from disco.core.env import disco_env
from disco.core.llm.config_store import ConfigStore
from disco.tools.sandbox import LocalSandboxService, SandboxConfig, SandboxSpec

CID = "replan-acceptance"
BUILD = (
    "Work in the sandbox at /workspace using your tools. Build a tiny static site:\n"
    "1. index.html with an <h1>Hello Disco</h1>.\n"
    "2. styles.css that makes the h1 blue, linked from index.html.\n"
    "Keep it minimal. When both files exist, you're done."
)
REPLAN = "Now add a footer.html partial that shows the year 2026, and link it from index.html."

_EXEC_WRITE_TOOLS = {"file_write", "file_str_replace", "shell"}


def _print_new(events: list, shown: int) -> int:
    for e in events[shown:]:
        if isinstance(e, MessageEvent):
            tag = "advisory" if "advisory" in (e.message.content or "").lower() else e.source.value
            print(f"  · {tag}: {e.message.content.strip()[:150]}")
        elif isinstance(e, PlanEvent):
            print(f"  ★ PLAN revision={getattr(e, 'revision', '?')} steps={len(e.steps)}")
        elif isinstance(e, ActionEvent):
            tc = e.tool_call
            print(
                f"  → ACTION {tc.tool_name if tc else '(finish)'} {str(tc.arguments if tc else {})[:90]}"
            )
        elif isinstance(e, StatusEvent):
            print(f"  [status] {e.status.value}{(' · ' + e.detail) if e.detail else ''}")
    return len(events)


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


async def _drive(runtime: ConversationRuntime, store, *, max_rounds: int, label: str) -> int:
    """Kick→step until terminal, auto-approving plans and risky actions. Returns
    the seq boundary (len events) at the end of this phase."""
    shown = len(await store.get_events(CID))
    for _r in range(max_rounds):
        runtime.kick(CID)
        task = runtime._run_registry.task(CID)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=240)
            except TimeoutError:
                print(f"  [TIMEOUT in {label}]")
                break
        events = await store.get_events(CID)
        shown = _print_new(events, shown)
        state = await store.get_state(CID)
        st = state.execution_status
        if st == ConversationStatus.AWAITING_PLAN_APPROVAL:
            print("  [approve] plan approved (human would decide here)")
            await runtime.approve_plan(CID)
            continue
        if st == ConversationStatus.WAITING_FOR_CONFIRMATION:
            print("  [approve] risky action auto-approved")
            await runtime.confirm(CID)
            continue
        if st in (
            ConversationStatus.FINISHED,
            ConversationStatus.ERROR,
            ConversationStatus.IDLE,
            ConversationStatus.STUCK,
            ConversationStatus.PAUSED,
        ):
            break
    return len(await store.get_events(CID))


async def main() -> None:
    cfg = ConfigStore().load()
    # Optional override of the driver's model_id (e.g. drop the flaky `:free`
    # provider pool for a reliable acceptance). Same model family, paid pool.
    _ov = disco_env("ACCEPT_MODEL_ID", "")
    if _ov:
        key = cfg.default_model
        cfg.models[key] = cfg.models[key].model_copy(update={"model_id": _ov})
        print(f"driver model_id OVERRIDE → {_ov}")
    print(
        f"driver default_model = {cfg.default_model} ({cfg.models[cfg.default_model].model_id})  (live disco-config.json)"
    )
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

    # ---- phase 1: the initial build ----
    await store.append(CID, _user(BUILD))
    print("\n=== PHASE 1: initial build ===")
    b1 = await _drive(runtime, store, max_rounds=24, label="build")
    phase1 = await store.get_state(CID)

    # ---- phase 2: the re-plan ----
    # Use the REAL revise/iterate entry point (request_plan -> enter_planning),
    # the same path the "revise plan" button and an iterate-with-new-idea take
    # (Dylan's trace shows RUNNING/planning on a post-FINISHED instruction).
    # A plain send_message (append+kick) would resume in EXECUTION mode and never
    # re-plan — that is NOT the path B2/B6 is about.
    print("\n=== PHASE 2: re-plan (new instruction via request_plan) ===")
    await runtime.request_plan(CID, REPLAN)
    await _drive(runtime, store, max_rounds=24, label="replan")

    events = await store.get_events(CID)
    final = await store.get_state(CID)

    # ---- assertions ----
    print("\n--- assertions ---")
    # B5: phase-1 build landed FINISHED, not PAUSED.
    b5 = phase1.execution_status == ConversationStatus.FINISHED
    print(
        f"  [{'PASS' if b5 else 'FAIL'}] B5  phase-1 build status = {phase1.execution_status.value} (want FINISHED, not PAUSED)"
    )

    # B4: no FALSE c18 'missing'/'does not exist' advisory for written files.
    advisories = [
        e
        for e in events
        if isinstance(e, MessageEvent)
        and e.source == EventSource.ENVIRONMENT
        and (
            "does not exist" in (e.message.content or "").lower()
            or (
                "c18" in (e.message.content or "").lower()
                and "not met" in (e.message.content or "").lower()
            )
        )
    ]
    b4 = len(advisories) == 0
    print(
        f"  [{'PASS' if b4 else 'FAIL'}] B4  false file-missing/C18-not-met advisories = {len(advisories)} (want 0)"
    )
    for a in advisories[:3]:
        print(f"        ! {a.message.content.strip()[:160]}")

    # B2+B6: a revision>=2 PlanEvent exists, AND it precedes any phase-2 execution write.
    plans = [(i, e) for i, e in enumerate(events) if isinstance(e, PlanEvent)]
    replan_plans = [(i, e) for i, e in plans if getattr(e, "revision", 1) >= 2 and i >= b1]
    b6 = len(replan_plans) > 0
    b2 = False
    if b6:
        first_replan_idx = replan_plans[0][0]
        first_exec_idx = next(
            (
                i
                for i, e in enumerate(events)
                if i >= b1
                and isinstance(e, ActionEvent)
                and e.tool_call is not None
                and e.tool_call.tool_name in _EXEC_WRITE_TOOLS
            ),
            None,
        )
        b2 = first_exec_idx is None or first_replan_idx < first_exec_idx
    print(f"  [{'PASS' if b6 else 'FAIL'}] B6  re-plan emitted a revision>=2 plan = {b6}")
    print(
        f"  [{'PASS' if b2 else 'FAIL'}] B2  plan precedes execution in the re-plan phase = {b2} (no free-build / no infinite spinner)"
    )

    ok = b2 and b4 and b5 and b6
    print(f"\n  final status: {final.execution_status.value}")
    print(f"  [{'PASS' if ok else 'CHECK'}] live acceptance — B2/B4/B5/B6")


if __name__ == "__main__":
    asyncio.run(main())
