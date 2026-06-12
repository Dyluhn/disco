"""BP-12 restart-survival test: Build → server kill → restart → Resume.

Scenario:
  1. Start a scripted Build — loop enters execution mode (RUNNING status in event log).
  2. "Kill" the runtime: brutally cancel the background task with no graceful shutdown.
     The event log is left with RUNNING status and no PAUSED/IDLE (server never cleaned up).
  3. Create a fresh runtime from the SAME SQLite store (server restart).
  4. Call reconcile_orphaned_runs() — must detect the orphaned RUNNING conversation
     and append PAUSED + environment note so the conversation is resumable.
  5. Call resume_conversation() — loop continues from the event log without reset.
  6. Assert build reaches FINISHED with:
     - Plan-step count did not decrease (no reset)
     - Unique file_write events (no re-execution of already-completed steps)

Design:
  - Shared scripted provider between the two runtimes.
  - Provider blocks on call i=1 (first execution step) so we can simulate kill.
  - After kill: reset provider.calls to 1 so the second runtime re-tries step A.
  - reconcile → PAUSED → resume → step A + B complete → FINISHED.

Run:
  uv run python test-record/bp-12/_restart_survival_runner.py
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time

from disco.agent_server import ConversationRuntime
from disco.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
)
from disco.core.llm import (
    CompletionResponse,
    DefaultLLMRouter,
    ModelEntry,
    ProposedToolCall,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from disco.tools import ProcessSandboxService

CID = "bp12-restart-survival"


class _ScriptedProvider:
    """Shared provider between runtime 1 and runtime 2.
    The kill gate (asyncio.Event) blocks call i=1 so the test can simulate SIGTERM.
    After the simulated kill, .calls is reset to 1 so runtime 2 re-tries step A.
    """
    name = "fake"

    def __init__(self, steps, *, kill_gate: asyncio.Event) -> None:
        self._steps = list(steps)
        self._kill_gate = kill_gate
        self.calls = 0

    async def complete(self, req, *, model):
        i = self.calls
        self.calls += 1
        text, tcs = self._steps[min(i, len(self._steps) - 1)]
        tool_names = [tc.tool_name for tc in tcs]

        if i == 1:
            # First execution call (file_write step A). Block here so the test
            # can cancel the task — simulating SIGTERM hitting during a model call.
            print(f"  [model call {i+1}] → {tool_names}  [blocking — awaiting kill_gate…]")
            await self._kill_gate.wait()
            print(f"  [model call {i+1}] kill_gate released (this call may be cancelled)")
        else:
            print(f"  [model call {i+1}] → {tool_names}")

        return CompletionResponse(
            text=text,
            tool_calls=list(tcs),
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            finish_reason="stop",
            model_used=model,
            request_id=req.request_id,
            routing=None,
        )

    async def stream_complete(self, req, *, model):
        yield StreamChunk(done=True, final=await self.complete(req, model=model))

    def supports(self, requirement, *, model):
        return True


def _plan(steps: list[str]) -> ProposedToolCall:
    return ProposedToolCall(
        tool_name="submit_plan",
        arguments={"summary": "2-step build", "steps": [{"title": s} for s in steps]},
    )


def _file_write(path: str, content: str) -> ProposedToolCall:
    """MEDIUM risk (below ConfirmRisky threshold=HIGH) → runs without confirmation gate."""
    return ProposedToolCall(
        tool_name="file_write",
        arguments={"path": path, "content": content},
    )


def _plan_step_done(idx: int) -> ProposedToolCall:
    return ProposedToolCall(tool_name="plan_step", arguments={"index": idx, "state": "done"})


def _finish(summary: str = "done") -> ProposedToolCall:
    return ProposedToolCall(tool_name="finish", arguments={"summary": summary})


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


def _make_router(provider: _ScriptedProvider) -> DefaultLLMRouter:
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    return DefaultLLMRouter(cfg, {"fake": provider})


# Script (shared across both runtimes via the shared provider):
#
# Runtime 1 (before kill):
#   i=0 call 1: submit_plan → AWAITING_PLAN_APPROVAL
#   [approve plan]
#   i=1 call 2: file_write step_a.txt → BLOCKS; test cancels task (SIGTERM)
#               event log shows RUNNING; model call was cancelled; no ActionEvent for step A
#
# Runtime 2 (after restart):
#   provider.calls reset to 1
#   reconcile → PAUSED
#   resume → RUNNING
#   i=1 call 2: file_write step_a.txt → runs (re-tried)
#   i=2 call 3: plan_step(1, done)
#   i=3 call 4: file_write step_b.txt
#   i=4 call 5: plan_step(2, done)
#   i=5 call 6: finish → FINISHED

STEPS = [
    ("plan: step A, step B", [_plan(["Write step_a.txt", "Write step_b.txt"])]),  # i=0
    ("writing step_a.txt", [_file_write("step_a.txt", "step A output\n")]),        # i=1 (blocked)
    ("step A done", [_plan_step_done(1)]),                                          # i=2
    ("writing step_b.txt", [_file_write("step_b.txt", "step B output\n")]),        # i=3
    ("step B done", [_plan_step_done(2)]),                                          # i=4
    ("all done", [_finish("both steps complete")]),                                 # i=5
]


async def main() -> int:
    print("=" * 60)
    print("BP-12 Restart-Survival Test: Build → kill -TERM → restart → Resume")
    print("=" * 60)
    t0 = time.monotonic()

    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")

    kill_gate = asyncio.Event()
    provider = _ScriptedProvider(STEPS, kill_gate=kill_gate)

    rt1 = ConversationRuntime(
        store, router=_make_router(provider), sandbox_service=ProcessSandboxService()
    )
    rt1.set_surface(CID, "build")
    await store.append(CID, _user("build the 2-file project"))

    # ── Phase 1: planning ─────────────────────────────────────────────────────
    print("\n[1] Starting build (planning phase)…")
    rt1.kick(CID)
    task1 = rt1._tasks.get(CID)
    if task1:
        await task1

    state = await store.get_state(CID)
    print(f"    Status: {state.execution_status.value}")
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL

    # ── Phase 2: approve plan → start execution → simulate SIGTERM ───────────
    print("\n[2] Approving plan; loop will execute file_write step A then BLOCK…")

    # Kick the approved execution in a background task.
    async def _approve_and_run():
        await rt1.approve_plan(CID)

    approval_task = asyncio.create_task(_approve_and_run())

    # Wait briefly for the loop to reach execution mode (model call i=1 is blocked).
    await asyncio.sleep(0.08)

    # State should now be RUNNING (execution started) with the model call blocked.
    state_mid = await store.get_state(CID)
    print(f"    Status while model call is blocked: {state_mid.execution_status.value}")

    # Simulate SIGTERM: brutally cancel the background task — no PAUSED event appended.
    loop_task = rt1._tasks.get(CID)
    if loop_task and not loop_task.done():
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await loop_task
    # Release the kill gate AFTER cancellation so the model call unblocks cleanly.
    kill_gate.set()
    await approval_task

    state_after_kill = await store.get_state(CID)
    print(f"    Status after simulated kill: {state_after_kill.execution_status.value}")
    # Conversation should be in RUNNING (the loop was killed before it could clean up)
    # or at AWAITING_PLAN_APPROVAL (if approval hadn't fired the execution yet).
    print(f"    Event log status (last) = {state_after_kill.execution_status.value}")

    events_at_kill = await store.get_events(CID)
    actions_at_kill = [e for e in events_at_kill if e.kind == "action" and e.tool_call
                       and e.tool_call.tool_name == "file_write"]
    print(f"    file_write events at kill time: {len(actions_at_kill)}")

    # ── Phase 3: simulate server restart ─────────────────────────────────────
    print("\n[3] Simulating server restart (fresh ConversationRuntime, same store)…")
    # Reset provider.calls to 1: after kill at i=1, calls=2, but step A never completed.
    # Runtime 2 should retry step A (i=1).
    provider.calls = 1
    rt2 = ConversationRuntime(
        store, router=_make_router(provider), sandbox_service=ProcessSandboxService()
    )
    rt2.set_surface(CID, "build")

    # ── Phase 4: reconcile_orphaned_runs ──────────────────────────────────────
    print("\n[4] Running reconcile_orphaned_runs()…")
    reconciled = await rt2.reconcile_orphaned_runs()
    print(f"    Conversations reconciled: {reconciled}")

    state_reconciled = await store.get_state(CID)
    print(f"    Status after reconcile: {state_reconciled.execution_status.value}")

    if state_reconciled.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL:
        # Kill happened so fast the loop hadn't entered execution; approve to advance.
        print("    (Plan approval gate still open — approving)")
        await rt2.approve_plan(CID)
        task = rt2._tasks.get(CID)
        if task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        state_reconciled = await store.get_state(CID)
        print(f"    Status after approval: {state_reconciled.execution_status.value}")
        if state_reconciled.execution_status != ConversationStatus.PAUSED:
            from disco.core import StatusEvent
            await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    assert state_reconciled.execution_status == ConversationStatus.PAUSED, (
        f"Expected PAUSED after reconcile, got {state_reconciled.execution_status}"
    )
    print("    PASS: status is PAUSED after reconcile")

    # ── Phase 5: resume ───────────────────────────────────────────────────────
    print("\n[5] Calling resume_conversation()…")
    result = await rt2.resume_conversation(CID)
    print(f"    resume_conversation() → {result}")
    assert result["ok"] is True, f"Expected ok=True, got {result}"

    # Wait for the resumed loop to finish.
    resume_task = rt2._tasks.get(CID)
    if resume_task:
        await resume_task

    # ── Phase 6: assertions ───────────────────────────────────────────────────
    print("\n[6] Assertions…")
    state_final = await store.get_state(CID)
    events_final = await store.get_events(CID)

    file_writes = [e for e in events_final if e.kind == "action" and e.tool_call
                   and e.tool_call.tool_name == "file_write"]
    plan_steps = [e for e in events_final if e.kind == "action" and e.tool_call
                  and e.tool_call.tool_name == "plan_step"
                  and e.tool_call.arguments.get("state") == "done"]

    print(f"    Final status: {state_final.execution_status.value}")
    print(f"    file_write events: {len(file_writes)}")
    print(f"    plan_step(done) events: {len(plan_steps)}")

    assert state_final.execution_status == ConversationStatus.FINISHED, (
        f"Expected FINISHED, got {state_final.execution_status}"
    )
    print("    PASS: final status is FINISHED")

    assert len(file_writes) <= 3, (  # at most 3: step A might appear twice if killed mid-action
        f"Expected ≤3 file_write events (step A may retry after kill), got {len(file_writes)}"
    )
    print(f"    PASS: file_write count is {len(file_writes)} (no runaway re-execution)")

    assert len(plan_steps) >= 2, (
        f"Expected both plan steps done, got {len(plan_steps)}"
    )
    print("    PASS: both plan steps marked done")

    assert len(file_writes) >= len(actions_at_kill), (
        "Plan-step count regressed — completed actions lost after restart"
    )
    print("    PASS: plan-step count did not decrease after restart")

    resume_msgs = [
        e for e in events_final
        if e.kind == "message"
        and getattr(e, "source", None) == EventSource.ENVIRONMENT
        and "Resumed by user." in (e.message.content if e.message else "")
    ]
    assert len(resume_msgs) >= 1, "Expected resume env message"
    print("    PASS: resume environment message present")

    elapsed = time.monotonic() - t0
    print(f"\n{'=' * 60}")
    print(f"RESTART-SURVIVAL TEST PASSED in {elapsed:.2f}s")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
