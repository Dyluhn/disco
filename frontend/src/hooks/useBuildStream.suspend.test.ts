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
import type { ConversationState, WSServerFrame } from "@/types/agent";
import { useBuildStream } from "./useBuildStream";

let sink: ((f: WSServerFrame) => void) | null = null;

vi.mock("@/api/agent", () => ({
  subscribeConversation: (_cid: string, onFrame: (f: WSServerFrame) => void): AgentHandle => {
    sink = onFrame;
    return { send: () => {}, cancel: () => {} };
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

// ---- file_stream agent_view_id gating ----------------------------------------

describe("useBuildStream — file_stream agent_view_id gating", () => {
  it("ignores a file_stream frame when no activeAgentViewId is set", () => {
    const session = { cid: "c_fs_noid", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({
        type: "file_stream",
        file_stream: {
          tool: "file_write",
          path: "index.html",
          index: 0,
          delta: "<h1>Hello</h1>",
          agent_view_id: "aview_noid",
        },
      });
    });

    expect(result.current.streamingFile).toBeNull();
  });

  it("ignores a file_stream frame whose agent_view_id differs from activeAgentViewId", () => {
    const session = { cid: "c_fs_wrong", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      // Establish an active view
      sink!({
        type: "event",
        event: {
          id: "ev_view",
          kind: "workspace_mutation",
          operation: "agent.view-admitted",
          agent_view_id: "aview_good",
          source: "system",
        },
      });
      // Stale file_stream with a different agent_view_id
      sink!({
        type: "file_stream",
        file_stream: {
          tool: "file_write",
          path: "index.html",
          index: 0,
          delta: "<h1>Stale</h1>",
          agent_view_id: "aview_stale",
        },
      });
    });

    expect(result.current.streamingFile).toBeNull();
  });

  it("renders delta from a file_stream frame whose agent_view_id matches activeAgentViewId", () => {
    const session = { cid: "c_fs_match", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({
        type: "event",
        event: {
          id: "ev_view2",
          kind: "workspace_mutation",
          operation: "agent.view-admitted",
          agent_view_id: "aview_write",
          source: "system",
        },
      });
      sink!({
        type: "file_stream",
        file_stream: {
          tool: "file_write",
          path: "out.txt",
          index: 1,
          delta: "Hello, world!",
          agent_view_id: "aview_write",
        },
      });
    });

    expect(result.current.streamingFile).toEqual({
      path: "out.txt",
      tool: "file_write",
      content: "Hello, world!",
    });
  });

  it("concatenates deltas for consecutive matching file_stream frames on the same path", () => {
    const session = { cid: "c_fs_concat", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({
        type: "event",
        event: {
          id: "ev_view3",
          kind: "workspace_mutation",
          operation: "agent.view-admitted",
          agent_view_id: "aview_concat",
          source: "system",
        },
      });
      sink!({
        type: "file_stream",
        file_stream: {
          tool: "file_edit",
          path: "src/app.ts",
          index: 2,
          delta: "return <Main/>;",
          field: "new",
          agent_view_id: "aview_concat",
        },
      });
      sink!({
        type: "file_stream",
        file_stream: {
          tool: "file_edit",
          path: "src/app.ts",
          index: 2,
          delta: "\nconsole.log('done');",
          field: "new",
          agent_view_id: "aview_concat",
        },
      });
    });

    expect(result.current.streamingFile).toEqual({
      path: "src/app.ts",
      tool: "file_edit",
      field: "new",
      content: "return <Main/>;\nconsole.log('done');",
    });
  });

  it("quarantines stale file_stream after a newer view-admitted supersedes the view", () => {
    const session = { cid: "c_fs_stale_view", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({
        type: "event",
        event: {
          id: "ev_v1",
          kind: "workspace_mutation",
          operation: "agent.view-admitted",
          agent_view_id: "aview_v1",
          source: "system",
        },
      });
      sink!({
        type: "file_stream",
        file_stream: {
          tool: "file_write",
          path: "f1.txt",
          index: 0,
          delta: "first view content",
          agent_view_id: "aview_v1",
        },
      });

      // Newer view admission supersedes v1
      sink!({
        type: "event",
        event: {
          id: "ev_v2",
          kind: "workspace_mutation",
          operation: "agent.view-admitted",
          agent_view_id: "aview_v2",
          source: "system",
        },
      });

      // Stale file_stream still tagged with old v1 — must be ignored
      sink!({
        type: "file_stream",
        file_stream: {
          tool: "file_write",
          path: "f1.txt",
          index: 0,
          delta: "stale from old view",
          agent_view_id: "aview_v1",
        },
      });
    });

    // The streamingFile from the first view should be cleared by the new admission
    expect(result.current.activeAgentViewId).toBe("aview_v2");
    expect(result.current.streamingFile).toBeNull();
  });
});

// ---- [12] v1 run-intent clears active view / stream, state frame restores ----

describe("useBuildStream — v1 run intent cycle", () => {
  it("clears activeAgentViewId and streamingFile on v1 run-intent", () => {
    const session = { cid: "c_v1_clear", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      // Establish view A1
      sink!({
        type: "event",
        event: {
          id: "ev_a1",
          kind: "workspace_mutation",
          operation: "agent.view-admitted",
          agent_view_id: "aview_a1",
          source: "system",
          seq: 5,
        },
      });
    });
    expect(result.current.activeAgentViewId).toBe("aview_a1");

    act(() => {
      // User sends a fresh instruction — run-intent clears the view
      sink!({
        type: "event",
        event: {
          id: "ev_i2",
          kind: "workspace_mutation",
          operation: "agent.run-intent.user-turn",
          run_protocol_version: 1,
          source: "system",
          seq: 6,
        },
      });
    });

    expect(result.current.activeAgentViewId).toBeNull();
    expect(result.current.streamingFile).toBeNull();
    expect(result.current.agentViewPending).toBe(true);
  });

  it("stale A1 file_stream after I2 cannot alter A2 state", () => {
    const session = { cid: "c_stale_after_i2", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      // A1 admitted
      sink!({
        type: "event",
        event: {
          id: "ev_a1",
          kind: "workspace_mutation",
          operation: "agent.view-admitted",
          agent_view_id: "aview_a1",
          source: "system",
          seq: 10,
        },
      });
      // I2 — new run intent
      sink!({
        type: "event",
        event: {
          id: "ev_i2",
          kind: "workspace_mutation",
          operation: "agent.run-intent.user-turn",
          run_protocol_version: 1,
          source: "system",
          seq: 11,
        },
      });
      // A2 admitted (newer view)
      sink!({
        type: "event",
        event: {
          id: "ev_a2",
          kind: "workspace_mutation",
          operation: "agent.view-admitted",
          agent_view_id: "aview_a2",
          source: "system",
          seq: 12,
        },
      });
      // Stale file_stream tagged with A1's view — must be ignored
      sink!({
        type: "file_stream",
        file_stream: {
          tool: "file_write",
          path: "out.txt",
          index: 0,
          delta: "stale content from A1",
          agent_view_id: "aview_a1",
        },
      });
    });

    expect(result.current.activeAgentViewId).toBe("aview_a2");
    expect(result.current.streamingFile).toBeNull();
  });

  it("state frame restores active_agent_view_id from server state", () => {
    const session = { cid: "c_state_restore", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({
        type: "state",
        state: {
          conversation_id: "c_state_restore",
          execution_status: "RUNNING",
          iteration: 3,
          max_iterations: 30,
          last_seq: 42,
          pending_action_id: null,
          pending_plan_id: null,
          extras: {},
          active_agent_view_id: "aview_restored",
          active_agent_view_seq: 40,
          agent_view_pending: false,
        },
      });
    });

    expect(result.current.activeAgentViewId).toBe("aview_restored");
    expect(result.current.activeAgentViewSeq).toBe(40);
    expect(result.current.agentViewPending).toBe(false);
  });

  it("stale A1 status/error after A2 cannot alter A2 state", () => {
    const session = { cid: "c_stale_status", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      // A2 is current view
      sink!({
        type: "event",
        event: {
          id: "ev_a2",
          kind: "workspace_mutation",
          operation: "agent.view-admitted",
          agent_view_id: "aview_a2",
          source: "system",
          seq: 20,
        },
      });
      // Stale A1 status event with different agent_view_id — must be ignored
      sink!({
        type: "event",
        event: {
          id: "ev_stale_status",
          kind: "status",
          status: "WAITING_FOR_CONFIRMATION",
          agent_view_id: "aview_a1",
          source: "system",
          seq: 21,
        },
      });
    });

    // The stale status must not change A2's state
    expect(result.current.activeAgentViewId).toBe("aview_a2");
    expect(result.current.pendingActionId).toBeNull();
    // Status stays IDLE (initial) since the stale event was correctly ignored
    expect(result.current.status).toBe("IDLE");
  });

  it("matching A2 file_stream still works after stale A1 events are ignored", () => {
    const session = { cid: "c_matching_a2", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      // A2 is current
      sink!({
        type: "event",
        event: {
          id: "ev_a2",
          kind: "workspace_mutation",
          operation: "agent.view-admitted",
          agent_view_id: "aview_a2",
          source: "system",
          seq: 30,
        },
      });
      // Stale A1 file_stream — ignored
      sink!({
        type: "file_stream",
        file_stream: {
          tool: "file_write",
          path: "stale.txt",
          index: 0,
          delta: "stale",
          agent_view_id: "aview_a1",
        },
      });
      // Matching A2 file_stream — should render
      sink!({
        type: "file_stream",
        file_stream: {
          tool: "file_write",
          path: "fresh.txt",
          index: 0,
          delta: "fresh content from A2",
          agent_view_id: "aview_a2",
        },
      });
    });

    expect(result.current.activeAgentViewId).toBe("aview_a2");
    expect(result.current.streamingFile).toEqual({
      path: "fresh.txt",
      tool: "file_write",
      content: "fresh content from A2",
    });
  });
});
