/**
 * The suspended-badge replay guard in useBuildStream (bp-13): a fresh connect
 * sends a state frame (with the extras.sandbox overlay + last_seq) and then
 * REPLAYS the whole event history. Historical RUNNING status events (seq <=
 * frameSeq) are the past — they must not erase the badge the frame just set.
 * A genuinely live RUNNING (seq beyond the frame, or no seq) still clears it.
 * Caught by run 3 of the live spec: the badge vanished on every page reload.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentHandle } from "@/api/agent";
import type { ConversationState, WSClientFrame, WSServerFrame } from "@/types/agent";
import { useBuildStream } from "./useBuildStream";

let sink: ((f: WSServerFrame) => void) | null = null;

vi.mock("@/api/agent", () => ({
  subscribeConversation: (_cid: string, onFrame: (f: WSServerFrame) => void): AgentHandle => {
    sink = onFrame;
    return { send: (_f: WSClientFrame) => {}, cancel: () => {} };
  },
}));

afterEach(() => {
  sink = null;
});

function suspendedState(cid: string, lastSeq: number): ConversationState {
  return {
    conversation_id: cid,
    execution_status: "FINISHED",
    iteration: 5,
    max_iterations: 30,
    last_seq: lastSeq,
    pending_action_id: null,
    pending_plan_id: null,
    extras: { sandbox: "suspended" },
  };
}

describe("useBuildStream — suspended badge vs history replay", () => {
  it("shows degraded connection state until the next real frame arrives", () => {
    const session = { cid: "c_connection", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({ type: "connection", state: "degraded" });
    });
    expect(result.current.connectionState).toBe("degraded");

    act(() => {
      sink!({ type: "connection", state: "connected" });
    });
    expect(result.current.connectionState).toBe("degraded");

    act(() => {
      sink!({ type: "state", state: suspendedState("c_connection", 1) });
    });
    expect(result.current.connectionState).toBe("connected");
  });

  it("keeps the badge through replayed RUNNING events (seq <= frameSeq)", () => {
    const session = { cid: "c_replay", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      // (1) connect: state frame says suspended, history runs to seq 12
      sink!({ type: "state", state: suspendedState("c_replay", 12) });
      // (2) history replay: the first build's RUNNING + FINISHED, all old seqs
      sink!({
        type: "event",
        event: { id: "s1", kind: "status", source: "system", status: "RUNNING", seq: 3 },
      });
      sink!({
        type: "event",
        event: { id: "s2", kind: "status", source: "system", status: "FINISHED", seq: 12 },
      });
    });

    expect(result.current.sandboxState).toBe("suspended");
    expect(result.current.status).toBe("FINISHED");
  });

  it("clears the badge on a genuinely live RUNNING (seq beyond the frame)", () => {
    const session = { cid: "c_live", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({ type: "state", state: suspendedState("c_live", 12) });
      // a follow-up message kicked the loop: this RUNNING is NEW
      sink!({
        type: "event",
        event: { id: "s3", kind: "status", source: "system", status: "RUNNING", seq: 13 },
      });
    });

    expect(result.current.sandboxState).toBeNull();
    expect(result.current.status).toBe("RUNNING");
  });

  it("treats a seq-less RUNNING as live (clears the badge)", () => {
    const session = { cid: "c_noseq", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({ type: "state", state: suspendedState("c_noseq", 12) });
      sink!({
        type: "event",
        event: { id: "s4", kind: "status", source: "system", status: "RUNNING" },
      });
    });

    expect(result.current.sandboxState).toBeNull();
  });
});
