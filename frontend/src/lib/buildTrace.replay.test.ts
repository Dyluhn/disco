/**
 * W9 — Frontend event-store REPLAY tests.
 *
 * Design principle (the whole point of W9): the test's INPUT is a real event-stream
 * fixture and its assertion is on the real derived OUTPUT — no mocking of the deriver
 * internals. Load a JSON fixture, cast to AgentEvent[], feed to the production selector,
 * assert on what the selector returns.
 *
 * Historical bug this suite locks in (#3): a finished small/local-model build showed 0/4
 * plan steps done because `deriveBuildProgress` early-returned an empty map when there was
 * NO `update_plan_progress` snapshot, skipping the FINISHED→all-done reconciliation.
 * The fix is in `buildTrace.ts`; these tests are the permanent guard.
 *
 * Run: npx vitest run src/lib/buildTrace.replay.test.ts
 */

import { describe, expect, it } from "vitest";
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import { deriveBuildProgress, deriveActivity, deriveLiveSignal } from "@/lib/buildTrace";

// Canonical minimized fixtures — each is a small hand-written event array plus an
// `expect` block and a `_why` note.  Load and cast: the derivers must tolerate
// fixtures that omit optional fields (that's intentional — minimal is durable).
import noPlanProgressFinished from "../../e2e-full/fixtures/no_plan_progress_finished.json";
import runningThenError from "../../e2e-full/fixtures/running_then_error.json";
import toolErrorVisible from "../../e2e-full/fixtures/tool_error_visible.json";
import finishedWithPartialPlanUpdates from "../../e2e-full/fixtures/finished_with_partial_plan_updates.json";
import cancelledRequest from "../../e2e-full/fixtures/cancelled_request.json";

/** Cast a fixture's raw events array to the real AgentEvent type.
 *  Fixtures are minimized and may omit optional EventBase fields — that is
 *  intentional.  The derivers must be robust to sparse inputs. */
function asEvents(fixture: { events: unknown[] }): AgentEvent[] {
  return fixture.events as unknown as AgentEvent[];
}

// ---------------------------------------------------------------------------
// Fixture: no_plan_progress_finished.json
// Bug #3: small/local model (Qwen) finished without ever calling
// update_plan_progress → deriveBuildProgress returned empty map → 0/4 shown.
// ---------------------------------------------------------------------------

describe("no_plan_progress_finished — #3 regression (small-model 0/4 after FINISHED)", () => {
  const evts = asEvents(noPlanProgressFinished);
  const status = noPlanProgressFinished.final_status as ConversationStatus;
  const { nsteps } = noPlanProgressFinished.expect;

  it("deriveBuildProgress: ALL steps reconcile to done on FINISHED with no snapshot", () => {
    // THE critical assertion: no update_plan_progress was emitted, but the build
    // FINISHED → the terminal reconciliation path MUST check off every step.
    // Pre-fix: returned Map{} (size 0); post-fix: Map{1:"done", 2:"done", 3:"done", 4:"done"}.
    const p = deriveBuildProgress(evts, status);
    expect(p.size).toBe(nsteps);
    for (let i = 1; i <= nsteps; i++) {
      expect(p.get(i)).toBe("done");
    }
  });

  it("deriveLiveSignal: FINISHED → idle (no active/spinning signal on a done build)", () => {
    const sig = deriveLiveSignal(evts, status);
    expect(sig.kind).toBe("idle");
  });

  it("deriveBuildProgress returns empty map while still RUNNING (no premature checks)", () => {
    // The fix must NOT leak into the running state — only a real FINISHED reconciles.
    const p = deriveBuildProgress(evts, "RUNNING");
    expect(p.size).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Fixture: running_then_error.json
// A RUNNING build that transitions to ERROR via a sandbox file-not-found.
// ---------------------------------------------------------------------------

describe("running_then_error — terminal ERROR is surfaced correctly", () => {
  const evts = asEvents(runningThenError);
  const status = runningThenError.final_status as ConversationStatus;
  const { stalled_step_index, failed_action_id, failed_action_error } = runningThenError.expect;

  it("deriveLiveSignal: ERROR → idle (not a confusingly active signal)", () => {
    const sig = deriveLiveSignal(evts, status);
    expect(sig.kind).toBe("idle");
  });

  it("deriveBuildProgress: active step becomes stalled on ERROR (honest about halted work)", () => {
    const p = deriveBuildProgress(evts, status);
    expect(p.get(stalled_step_index)).toBe("stalled");
  });

  it("deriveActivity: the errored action appears as status=failed", () => {
    const items = deriveActivity(evts, null, status);
    const item = items.find((i) => i.id === failed_action_id);
    expect(item).toBeDefined();
    expect(item?.status).toBe("failed");
  });

  it("deriveActivity: the error text is in expandable.error (visible to the user on drill-down)", () => {
    const items = deriveActivity(evts, null, status);
    const item = items.find((i) => i.id === failed_action_id);
    expect(item?.expandable?.error).toBe(failed_action_error);
  });
});

// ---------------------------------------------------------------------------
// Fixture: tool_error_visible.json
// An agent_error event (tool call crashed) must surface in deriveActivity as
// status=failed with the error text accessible in expandable.error.
// Distinct from a success=false observation — agent_error means the tool
// itself threw, not merely returned a non-zero exit code.
// ---------------------------------------------------------------------------

describe("tool_error_visible — agent_error reflected in activity feed", () => {
  const evts = asEvents(toolErrorVisible);
  const status = toolErrorVisible.final_status as ConversationStatus;
  const { action_id, expected_error } = toolErrorVisible.expect;

  it("deriveActivity: the shell action with agent_error has status=failed", () => {
    const items = deriveActivity(evts, null, status);
    const item = items.find((i) => i.id === action_id);
    expect(item).toBeDefined();
    expect(item?.status).toBe("failed");
  });

  it("deriveActivity: the error text is in expandable.error", () => {
    const items = deriveActivity(evts, null, status);
    const item = items.find((i) => i.id === action_id);
    expect(item?.expandable?.error).toBe(expected_error);
  });

  it("deriveActivity: the action is still present in the feed (not silently dropped)", () => {
    const items = deriveActivity(evts, null, status);
    // At least one item; the failed shell action must not be swallowed.
    const shellItems = items.filter((i) => i.expandable?.tool_name === "shell");
    expect(shellItems.length).toBeGreaterThanOrEqual(1);
  });

  it("deriveLiveSignal: ERROR → idle", () => {
    const sig = deriveLiveSignal(evts, status);
    expect(sig.kind).toBe("idle");
  });
});

// ---------------------------------------------------------------------------
// Fixture: finished_with_partial_plan_updates.json
// A capable model emitted a partial snapshot (1 done, 1 active, 1 pending)
// but NO final 100%-done snapshot before FINISHED.  All steps must still
// reconcile to done — the stale partial snapshot must not leave "1/3" in UI.
// ---------------------------------------------------------------------------

describe("finished_with_partial_plan_updates — stale partial snapshot + FINISHED → all done", () => {
  const evts = asEvents(finishedWithPartialPlanUpdates);
  const status = finishedWithPartialPlanUpdates.final_status as ConversationStatus;
  const { nsteps } = finishedWithPartialPlanUpdates.expect;

  it("deriveBuildProgress: all steps done even though last snapshot was partial (not 1/3)", () => {
    const p = deriveBuildProgress(evts, status);
    expect(p.size).toBe(nsteps);
    for (let i = 1; i <= nsteps; i++) {
      expect(p.get(i)).toBe("done");
    }
  });

  it("deriveLiveSignal: FINISHED → idle", () => {
    const sig = deriveLiveSignal(evts, status);
    expect(sig.kind).toBe("idle");
  });

  it("deriveBuildProgress during RUNNING: partial snapshot is applied as-is (not prematurely all-done)", () => {
    // Re-derive with RUNNING to confirm terminal reconciliation is status-gated.
    const p = deriveBuildProgress(evts, "RUNNING");
    // step 1 was in the snapshot as "done"
    expect(p.get(1)).toBe("done");
    // step 2 was "active" — must NOT be done while still RUNNING
    expect(p.get(2)).not.toBe("done");
  });
});

// ---------------------------------------------------------------------------
// Fixture: cancelled_request.json
// User cancelled a build mid-run (loop stops; status → IDLE).
// ---------------------------------------------------------------------------

describe("cancelled_request — mid-run cancellation (IDLE)", () => {
  const evts = asEvents(cancelledRequest);
  const status = cancelledRequest.final_status as ConversationStatus;
  const { stalled_step_index, done_step_index } = cancelledRequest.expect;

  it("deriveLiveSignal: IDLE → idle (no spinning signal on a halted build)", () => {
    const sig = deriveLiveSignal(evts, status);
    expect(sig.kind).toBe("idle");
  });

  it("deriveBuildProgress: the active step becomes stalled (honest about unfinished work)", () => {
    const p = deriveBuildProgress(evts, status);
    expect(p.get(stalled_step_index)).toBe("stalled");
  });

  it("deriveBuildProgress: the already-done step stays done (cancel does not regress done steps)", () => {
    const p = deriveBuildProgress(evts, status);
    expect(p.get(done_step_index)).toBe("done");
  });
});
