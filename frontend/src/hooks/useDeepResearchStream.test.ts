/**
 * Unit tests for the useDeepResearchStream reducer (exported for testability).
 *
 * WALK-08 (A4): dispatch reset when session is null (prevent stale events).
 * WALK-11 (B2): follow_up / follow_up_complete StatusEvents must NOT overwrite
 *               the main `status` field.
 * v2 (gateless): the reducer holds NO plan-gate state — see the last block.
 */

import { describe, expect, it } from "vitest";
import { initial, reducer } from "./useDeepResearchStream";
import type { ErrorEvent, StatusEvent } from "@/types/agent";
import errorEvent from "@/lib/__fixtures__/deep-research-error-event.json";

it("a late steer refusal cannot turn a healthy writing run into an error", () => {
  const state = { ...initial, status: "RUNNING" as const };
  expect(reducer(state, {
    type: "frame",
    frame: { type: "error", error: { detail: "research_steering_closed" } },
  })).toEqual(state);
});

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

// ---- F5: a reconnect during a follow-up must not re-open the run view --------
//
// The frames below are the ones captured on the isolated stack
// (F5-evidence/before/ws-frames.txt, conv_d27b5c…b15ad810, 89 grounding
// passages). The follow-up's grounding pass ran 95 s without emitting an event,
// so the live socket's own 45 s stale-frame watchdog closed and reopened the
// connection TWICE at `last_seq=139`, and each reopen delivered a state snapshot
// saying RUNNING — the follow-up's status, projected with no detail attached.
// `status` then stayed RUNNING through the answer, so the finished report kept
// rendering as a stalled run (Stop/Kill up, "no signal for 1:00") and the
// follow-up thread never mounted. That is the assertion the final live spec
// failed on: `deep-research-truth.spec.ts:199`.

describe("reducer — F5 reconnect during a follow-up", () => {
  function stateFrame(status: string, lastSeq: number) {
    return {
      type: "state" as const,
      state: {
        conversation_id: "conv_d27b5c5b6c3a4537a87b2da4b15ad810",
        execution_status: status as StatusEvent["status"],
        iteration: 0,
        max_iterations: 500,
        last_seq: lastSeq,
        pending_action_id: null,
        pending_plan_id: null,
      },
    };
  }

  it("keeps the report on screen when the watchdog reconnects mid-follow-up", () => {
    // seq 135: the finished main run, as the first connection replayed it.
    let state = reducer(initial, {
      type: "frame",
      frame: { type: "event", event: makeStatusEvent("s135", "FINISHED") },
    });
    // seq 137: the follow-up opens.
    state = reducer(state, {
      type: "frame",
      frame: { type: "event", event: makeStatusEvent("s137", "RUNNING", "follow_up") },
    });
    expect(state.status).toBe("FINISHED");

    // 53.2 s: socket closed by the stale-frame watchdog. 54.3 s: the new
    // socket's snapshot. This is the frame that used to flip the surface.
    state = reducer(state, { type: "frame", frame: stateFrame("RUNNING", 139) });
    expect(state.status).toBe("FINISHED");
    // 99.3 s / 100.4 s: it happened a second time in the same follow-up.
    state = reducer(state, { type: "frame", frame: stateFrame("RUNNING", 139) });
    expect(state.status).toBe("FINISHED");

    // seq 142: the follow-up's own FINISHED, routed to followUpStatus.
    state = reducer(state, {
      type: "frame",
      frame: { type: "event", event: makeStatusEvent("s142", "FINISHED", "follow_up_complete") },
    });
    expect(state.status).toBe("FINISHED");
    expect(state.followUpStatus).toBe("follow_up_complete");
  });

  it("still takes the snapshot's status once the follow-up is over", () => {
    let state = reducer(initial, {
      type: "frame",
      frame: { type: "event", event: makeStatusEvent("s142", "FINISHED", "follow_up_complete") },
    });
    // A later reconnect on a genuinely restarted run must still be believed —
    // the guard is scoped to a follow-up that is actually in flight.
    state = reducer(state, { type: "frame", frame: stateFrame("RUNNING", 200) });
    expect(state.status).toBe("RUNNING");
  });

  it("still takes the snapshot's status on a cold open", () => {
    const state = reducer(initial, { type: "frame", frame: stateFrame("RUNNING", 12) });
    expect(state.status).toBe("RUNNING");
  });
});

// ---- no plan gate (v2 regression guard) -------------------------------------
//
// Deep Research is gateless: submitting starts the research and the model's
// brief is its first visible output. The reducer used to special-case
// AWAITING_PLAN_APPROVAL and stash a `pendingPlanId` for the approve/revise
// gate. That state is GONE, and this block is the guard against it creeping
// back — a status the deep-research engine never emits must be carried as a
// plain status, with no gate bookkeeping attached.

describe("reducer — no plan gate (v2 regression)", () => {
  it("AWAITING_PLAN_APPROVAL is carried as a plain status with no gate state", () => {
    const state = reducer(initial, {
      type: "frame",
      frame: {
        type: "event",
        event: makeStatusEvent("s1", "AWAITING_PLAN_APPROVAL", "plan_evt_abc"),
      },
    });
    expect(state.status).toBe("AWAITING_PLAN_APPROVAL");
    expect(state).not.toHaveProperty("pendingPlanId");
    expect(state.followUpStatus).toBeNull();
  });

  it("a `state` frame carries only the execution status, not a pending plan id", () => {
    const state = reducer(initial, {
      type: "frame",
      frame: {
        type: "state",
        state: {
          conversation_id: "c1",
          execution_status: "RUNNING",
          iteration: 1,
          max_iterations: 30,
          last_seq: 2,
          pending_action_id: null,
          pending_plan_id: "plan_evt_abc",
        },
      },
    });
    expect(state.status).toBe("RUNNING");
    expect(state).not.toHaveProperty("pendingPlanId");
  });
});

// ---- L26 follow-up: the terminal failure survives as FIELDS -------------------

describe("reducer — ErrorEvent.failure", () => {
  const event = errorEvent as unknown as ErrorEvent;

  it("keeps the four parts, not just the sentence", () => {
    const state = reducer(initial, { type: "frame", frame: { type: "event", event } });
    expect(state.status).toBe("ERROR");
    expect(state.error).toBe(event.detail);
    expect(state.failure).toEqual(event.failure);
  });

  it("carries no failure for an event that named no boundary", () => {
    const bare: ErrorEvent = { id: "e1", kind: "error", detail: "conversation error" };
    const state = reducer(initial, { type: "frame", frame: { type: "event", event: bare } });
    expect(state.error).toBe("conversation error");
    expect(state.failure).toBeNull();
  });

  it("drops a previous run's failure when the TRANSPORT is what failed", () => {
    const failed = reducer(initial, { type: "frame", frame: { type: "event", event } });
    const state = reducer(failed, {
      type: "frame",
      frame: { type: "error", error: { detail: "socket closed" } },
    });
    expect(state.error).toBe("socket closed");
    expect(state.failure).toBeNull();
  });
});

// ---- UI-33 connection state ---------------------------------------------------

describe("reducer — live socket state", () => {
  it("records the socket going degraded so the run view can stop claiming Live", () => {
    const state = reducer(
      { ...initial, status: "RUNNING" },
      { type: "frame", frame: { type: "connection", state: "degraded" } },
    );
    expect(state.connectionState).toBe("degraded");
    // A dropped stream is not a failed run: nothing else about it moves.
    expect(state.status).toBe("RUNNING");
    expect(state.error).toBeNull();
  });

  it("treats the next delivered frame as proof the stream is back", () => {
    const degraded = reducer(
      { ...initial, status: "RUNNING" },
      { type: "frame", frame: { type: "connection", state: "degraded" } },
    );
    const recovered = reducer(degraded, {
      type: "frame",
      frame: { type: "event", event: makeStatusEvent("s9", "RUNNING") },
    });
    expect(recovered.connectionState).toBe("connected");
  });
});
