"""BP-12 integration test: scripted Build → graceful Stop → Resume.

Asserts:
  1. After Stop (PAUSED), resume_conversation() returns ok=True.
  2. The loop continues from the event log: plan-step pointer did NOT reset and
     no completed action is re-executed.
  3. The run reaches FINISHED with all plan steps completed.

Design:
  - 2-step plan. Step A uses file_write (productive, no gate). Step B uses file_write.
  - After step A's file_write executes, the scripted provider BLOCKS on an asyncio.Event.
  - The test appends StatusEvent(PAUSED) to the store, then unblocks the provider.
  - Step B's file_write executes next (before the loop reads PAUSED at the next checkpoint).
  - The loop exits on seeing PAUSED; plan_step(1,done) is done, plan_step(2,done) is NOT yet.
  - resume_conversation() is called; loop continues with plan_step(2,done) + finish.

Run:
  uv run python test-record/bp-12/_integration_runner.py
"""

from __future__ import annotations

import asyncio
import sys
import time

from perpleximanus.agent_server import ConversationRuntime
from perpleximanus.core import (
    ConversationStatus,
    EventSource,
    LLMMessage,
    MessageEvent,
    SqliteEventStore,
    StatusEvent,
)
from perpleximanus.core.llm import (
    CompletionResponse,
    DefaultLLMRouter,
    ModelEntry,
    ProposedToolCall,
    RouterConfig,
    StreamChunk,
    TokenUsage,
)
from perpleximanus.tools import ProcessSandboxService

CID = "bp12-integration-test"


class _ScriptedProvider:
    name = "fake"

    def __init__(self, steps, *, step_a_done: asyncio.Event, resume_gate: asyncio.Event) -> None:
        self._steps = list(steps)
        self._step_a_done = step_a_done   # set after call 3 (plan_step done for step A)
        self._resume_gate = resume_gate   # blocks call 4 (file_write for step B)
        self.calls = 0

    async def complete(self, req, *, model):
        i = self.calls
        self.calls += 1
        text, tcs = self._steps[min(i, len(self._steps) - 1)]
        tool_names = [tc.tool_name for tc in tcs]

        if i == 2:
            # call 3 — plan_step(1, done) just returned. Signal the test that
            # step A is fully done (its file_write + plan_step both ran).
            self._step_a_done.set()
            print(f"  [model call {i+1}] → {tool_names}  [signaled step_a_done]")
        elif i == 3:
            # call 4 — step B's file_write. Block until the test has injected PAUSED.
            print(f"  [model call {i+1}] → {tool_names}  [blocking on resume_gate…]")
            await self._resume_gate.wait()
            print(f"  [model call {i+1}] unblocked")
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
    """file_write is productive (not in _NON_PRODUCTIVE_TOOLS) and is rated MEDIUM risk
    by the RuleBasedAnalyzer — below the ConfirmRisky(threshold=HIGH) gate — so it runs
    without a human confirmation step."""
    return ProposedToolCall(
        tool_name="file_write",
        arguments={"path": path, "content": content},
    )


def _plan_step_done(idx: int) -> ProposedToolCall:
    return ProposedToolCall(tool_name="plan_step", arguments={"index": idx, "state": "done"})


def _finish(summary: str = "build complete") -> ProposedToolCall:
    return ProposedToolCall(tool_name="finish", arguments={"summary": summary})


def _user(content: str) -> MessageEvent:
    return MessageEvent(source=EventSource.USER, message=LLMMessage(role="user", content=content))


# Script:
#  i=0 call 1: submit_plan (2 steps)   → AWAITING_PLAN_APPROVAL
#  [approve_plan]
#  i=1 call 2: file_write step_a.txt   → runs (MEDIUM risk, no gate)
#  i=2 call 3: plan_step(1, done)      → runs; sets step_a_done event
#  i=3 call 4: file_write step_b.txt   → BLOCKS; test injects PAUSED; unblocked; runs
#              [loop sees PAUSED at next checkpoint; exits]
#  [resume_conversation → RUNNING]
#  i=4 call 5: plan_step(2, done)      → runs; all steps now done
#  i=5 call 6: finish                  → FINISHED

STEPS = [
    ("plan: step A, step B", [_plan(["Write step_a.txt", "Write step_b.txt"])]),  # i=0
    ("writing step_a.txt", [_file_write("step_a.txt", "step A output\n")]),        # i=1
    ("step A done", [_plan_step_done(1)]),                                          # i=2
    ("writing step_b.txt", [_file_write("step_b.txt", "step B output\n")]),        # i=3
    ("step B done", [_plan_step_done(2)]),                                          # i=4
    ("all done", [_finish("both steps complete")]),                                 # i=5
]


async def main() -> int:
    print("=" * 60)
    print("BP-12 Integration Test: Build → Stop → Resume")
    print("=" * 60)
    t0 = time.monotonic()

    store = SqliteEventStore(":memory:")
    store.create_conversation(CID, owner_id="local")

    step_a_done = asyncio.Event()
    resume_gate = asyncio.Event()

    provider = _ScriptedProvider(STEPS, step_a_done=step_a_done, resume_gate=resume_gate)
    cfg = RouterConfig(
        models={"m": ModelEntry(model_id="m", provider="fake", context_window=8192)},
        default_model="m",
    )
    router = DefaultLLMRouter(cfg, {"fake": provider})
    rt = ConversationRuntime(store, router=router, sandbox_service=ProcessSandboxService())
    rt.set_surface(CID, "build")
    await store.append(CID, _user("build the 2-file project"))

    # ── Phase 1: planning ────────────────────────────────────────────────────
    print("\n[1] Planning phase…")
    rt.kick(CID)
    task = rt._tasks.get(CID)
    if task:
        await task

    state = await store.get_state(CID)
    print(f"    Status: {state.execution_status.value}")
    assert state.execution_status == ConversationStatus.AWAITING_PLAN_APPROVAL, (
        f"Expected AWAITING_PLAN_APPROVAL, got {state.execution_status}"
    )

    # ── Phase 2: execute step A, inject PAUSED while step B's model call blocks ──
    print("\n[2] Approving plan (loop will run step A then block on step B)…")

    async def _run_with_cancel():
        """Run the approved loop concurrently with the test's PAUSED injection."""
        await rt.approve_plan(CID)

    # Start the approve+loop in a background task so we can race it.
    loop_outer = asyncio.create_task(_run_with_cancel())

    # Wait for step A to complete (plan_step(1,done) observation appended to store).
    await step_a_done.wait()

    # Verify step A work is in the event log.
    events_mid = await store.get_events(CID)
    step_a_actions = [
        e for e in events_mid if e.kind == "action"
        and e.tool_call
        and e.tool_call.tool_name == "file_write"
        and "step_a" in (e.tool_call.arguments.get("path") or "")
    ]
    print(f"    Step A file_write events in log: {len(step_a_actions)}")
    assert len(step_a_actions) == 1, f"Expected 1 step_a action, got {len(step_a_actions)}"

    plan_steps_done_mid = [
        e for e in events_mid if e.kind == "action"
        and e.tool_call
        and e.tool_call.tool_name == "plan_step"
        and e.tool_call.arguments.get("state") == "done"
    ]
    n_done_before_pause = len(plan_steps_done_mid)
    print(f"    Plan steps done before PAUSED injection: {n_done_before_pause}")
    assert n_done_before_pause == 1, f"Expected 1 plan step done, got {n_done_before_pause}"

    # Inject PAUSED (simulates the user clicking Stop after step A completed).
    print("    Injecting StatusEvent(PAUSED)…")
    await store.append(CID, StatusEvent(status=ConversationStatus.PAUSED))

    # Unblock the provider (step B's model call will return; its file_write executes;
    # but the loop exits when it hits the PAUSED checkpoint on the NEXT iteration).
    resume_gate.set()

    # Wait for the loop to see PAUSED and exit.
    await loop_outer
    loop_task = rt._tasks.get(CID)
    if loop_task and not loop_task.done():
        await loop_task

    state = await store.get_state(CID)
    print(f"    Status after Stop: {state.execution_status.value}")
    assert state.execution_status == ConversationStatus.PAUSED, (
        f"Expected PAUSED, got {state.execution_status}"
    )

    events_paused = await store.get_events(CID)
    file_writes_before_resume = [
        e for e in events_paused if e.kind == "action"
        and e.tool_call
        and e.tool_call.tool_name == "file_write"
    ]
    plan_steps_before_resume = [
        e for e in events_paused if e.kind == "action"
        and e.tool_call
        and e.tool_call.tool_name == "plan_step"
        and e.tool_call.arguments.get("state") == "done"
    ]
    print(f"    file_write events before resume: {len(file_writes_before_resume)}")
    print(f"    plan_step(done) events before resume: {len(plan_steps_before_resume)}")

    # ── Phase 3: resume ──────────────────────────────────────────────────────
    print("\n[3] Calling resume_conversation()…")
    result = await rt.resume_conversation(CID)
    print(f"    resume_conversation() → {result}")
    assert result["ok"] is True, f"Expected ok=True, got {result}"
    assert result["status"] == "RUNNING"

    # ── Phase 4: wait for resumed loop to finish ──────────────────────────────
    print("\n[4] Waiting for resumed loop to reach FINISHED…")
    resume_task = rt._tasks.get(CID)
    if resume_task:
        await resume_task

    state_final = await store.get_state(CID)
    print(f"    Final status: {state_final.execution_status.value}")

    events_final = await store.get_events(CID)
    file_writes_final = [
        e for e in events_final if e.kind == "action"
        and e.tool_call
        and e.tool_call.tool_name == "file_write"
    ]
    plan_steps_final = [
        e for e in events_final if e.kind == "action"
        and e.tool_call
        and e.tool_call.tool_name == "plan_step"
        and e.tool_call.arguments.get("state") == "done"
    ]

    print(f"    file_write events final: {len(file_writes_final)}")
    print(f"    plan_step(done) events final: {len(plan_steps_final)}")

    # ── Phase 5: assertions ───────────────────────────────────────────────────
    print("\n[5] Assertions…")

    # Build reached FINISHED.
    assert state_final.execution_status == ConversationStatus.FINISHED, (
        f"Expected FINISHED, got {state_final.execution_status}"
    )
    print("    PASS: final status is FINISHED")

    # No re-execution: file_write count must not exceed the number of plan steps.
    assert len(file_writes_final) <= 2, (
        f"Expected at most 2 file_write events, got {len(file_writes_final)} — re-execution?"
    )
    print(f"    PASS: file_write count is {len(file_writes_final)} (no duplicates)")

    # Plan-step pointer did not reset: count only grows.
    assert len(plan_steps_final) >= len(plan_steps_before_resume), (
        f"Plan-step count decreased: {len(plan_steps_final)} < {len(plan_steps_before_resume)}"
    )
    print(f"    PASS: plan_step(done) {len(plan_steps_before_resume)} → {len(plan_steps_final)} (no reset)")

    # Both plan steps are done.
    assert len(plan_steps_final) >= 2, (
        f"Expected both plan steps done, got {len(plan_steps_final)}"
    )
    print("    PASS: all plan steps marked done")

    # Resume env message appended exactly once.
    resume_msgs = [
        e for e in events_final
        if e.kind == "message"
        and getattr(e, "source", None) == EventSource.ENVIRONMENT
        and "Resumed by user." in (e.message.content if e.message else "")
    ]
    assert len(resume_msgs) == 1, f"Expected 1 resume env message, got {len(resume_msgs)}"
    print("    PASS: exactly one 'Resumed by user.' environment message")

    elapsed = time.monotonic() - t0
    print(f"\n{'=' * 60}")
    print(f"INTEGRATION TEST PASSED in {elapsed:.2f}s")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
