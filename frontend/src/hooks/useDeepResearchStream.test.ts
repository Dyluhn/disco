/**
 * Unit tests for the useDeepResearchStream reducer (exported for testability).
 *
 * WALK-08 (A4): dispatch reset when session is null (prevent stale events).
 * WALK-11 (B2): follow_up / follow_up_complete StatusEvents must NOT overwrite
 *               the main `status` field.
 */

import { describe, expect, it } from "vitest";
import { initial, reducer } from "./useDeepResearchStream";
import type { StatusEvent } from "@/types/agent";

// ---- helpers -----------------------------------------------------------------

function makeStatusEvent(
  id: string,
  status: StatusEvent["status"],
  detail?: string | null,
): StatusEvent {
  return { id, kind: "status", seq: 1, status, detail: detail ?? null };
}

// ---- WALK-08 reset -----------------------------------------------------------

describe("reducer — reset", () => {
  it("WALK-08: reset action returns the initial state regardless of prior state", () => {
    // First land some events to dirty the state
    let state = reducer(initial, {
      type: "frame",
      frame: {
        type: "event",
        event: makeStatusEvent("s1", "RUNNING"),
      },
    });
    expect(state.status).toBe("RUNNING");

    // reset should clear everything
    state = reducer(state, { type: "reset" });
    expect(state).toEqual(initial);
    expect(state.status).toBe("IDLE");
    expect(state.followUpStatus).toBeNull();
    expect(state.events).toHaveLength(0);
  });
});

// ---- WALK-11 follow-up status isolation -------------------------------------

describe("reducer — WALK-11 followUpStatus decoupling", () => {
  it("a plain RUNNING StatusEvent sets status=RUNNING and leaves followUpStatus null", () => {
    const state = reducer(initial, {
      type: "frame",
      frame: { type: "event", event: makeStatusEvent("s1", "RUNNING") },
    });
    expect(state.status).toBe("RUNNING");
    expect(state.followUpStatus).toBeNull();
  });

  it("WALK-11: StatusEvent(detail='follow_up') does NOT overwrite main status", () => {
    // Start in FINISHED state (plan run done)
    let state = reducer(initial, {
      type: "frame",
      frame: { type: "event", event: makeStatusEvent("s1", "FINISHED") },
    });
    expect(state.status).toBe("FINISHED");

    // Follow-up kicks: the backend emits StatusEvent(RUNNING, detail="follow_up")
    state = reducer(state, {
      type: "frame",
      frame: {
        type: "event",
        event: makeStatusEvent("s2", "RUNNING", "follow_up"),
      },
    });

    // CRITICAL: status must remain FINISHED — not overwritten to RUNNING.
    // If it were RUNNING, DeepProgressStrip would re-spin the plan loader.
    expect(state.status).toBe("FINISHED");
    expect(state.followUpStatus).toBe("follow_up");
  });

  it("WALK-11: StatusEvent(detail='follow_up_complete') sets followUpStatus without touching status", () => {
    let state = reducer(initial, {
      type: "frame",
      frame: { type: "event", event: makeStatusEvent("s1", "FINISHED") },
    });

    state = reducer(state, {
      type: "frame",
      frame: {
        type: "event",
        event: makeStatusEvent("s2", "FINISHED", "follow_up_complete"),
      },
    });

    expect(state.status).toBe("FINISHED");
    expect(state.followUpStatus).toBe("follow_up_complete");
  });

  it("WALK-11: a non-follow-up StatusEvent clears followUpStatus back to null", () => {
    // Set followUpStatus via a follow_up event
    let state = reducer(initial, {
      type: "frame",
      frame: {
        type: "event",
        event: makeStatusEvent("s1", "RUNNING", "follow_up"),
      },
    });
    expect(state.followUpStatus).toBe("follow_up");

    // Now a normal FINISHED event should clear it
    state = reducer(state, {
      type: "frame",
      frame: { type: "event", event: makeStatusEvent("s2", "FINISHED") },
    });
    expect(state.status).toBe("FINISHED");
    expect(state.followUpStatus).toBeNull();
  });

  it("WALK-11: follow_up events accumulate in events[] even though status is preserved", () => {
    const state = reducer(initial, {
      type: "frame",
      frame: {
        type: "event",
        event: makeStatusEvent("s1", "RUNNING", "follow_up"),
      },
    });
    // The event is still upserted into the log (for replay/sourcing)
    expect(state.events).toHaveLength(1);
    expect(state.events[0].kind).toBe("status");
  });
});

// ---- AWAITING_PLAN_APPROVAL pendingPlanId (regression guard) ----------------

describe("reducer — pendingPlanId (regression)", () => {
  it("AWAITING_PLAN_APPROVAL still sets pendingPlanId from detail", () => {
    const state = reducer(initial, {
      type: "frame",
      frame: {
        type: "event",
        event: makeStatusEvent("s1", "AWAITING_PLAN_APPROVAL", "plan_evt_abc"),
      },
    });
    expect(state.status).toBe("AWAITING_PLAN_APPROVAL");
    expect(state.pendingPlanId).toBe("plan_evt_abc");
    expect(state.followUpStatus).toBeNull();
  });
});
