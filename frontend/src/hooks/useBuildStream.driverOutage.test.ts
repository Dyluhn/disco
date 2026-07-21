/**
 * Honest driver-outage surfacing (Option B): the backend labels a provider
 * usage/rate-limit (HTTP 429) exhaustion pause into the blocked-landing
 * StatusEvents' non-semantic meta; the hook derives `driverOutage` from the
 * EVENT LOG (not a mutable reducer field) so it survives WS history replay on
 * reload/reconnect, and a newer RUNNING status naturally supersedes it.
 *
 * Also covers the ERROR-detail drop: the latest StatusEvent(ERROR).detail now
 * reaches `error` (replay path included) instead of the generic fallback.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentHandle } from "@/api/agent";
import type { AgentEvent, ConversationState, WSServerFrame } from "@/types/agent";
import {
  deriveDriverOutage,
  deriveRunErrorDetail,
  useBuildStream,
} from "./useBuildStream";

let sink: ((f: WSServerFrame) => void) | null = null;

vi.mock("@/api/agent", () => ({
  subscribeConversation: (_cid: string, onFrame: (f: WSServerFrame) => void): AgentHandle => {
    sink = onFrame;
    return { send: () => {}, cancel: () => {} };
  },
  resumeConversation: async () => {},
}));

afterEach(() => {
  sink = null;
});

// The backend's real landing meta shape (turn_control._blocked_meta + the
// driver's extra_meta labels; driver_error is the adapter's SANITIZED line —
// verbatim from the live soak's captured evidence, never a raw body).
const OUTAGE_META = {
  blocked_landing: true,
  blocked_reason: "driver-unavailable",
  legacy_status: "PAUSED",
  legacy_detail: "driver-unavailable",
  driver_error: "provider opencode-go returned HTTP 429 type=GoUsageLimitError",
  driver_error_kind: "LLMTransientError",
  driver_error_provider: "opencode-go",
  driver_error_http_status: 429,
};

// A timeout/network exhaustion: same landing, NO http status → generic copy.
const TIMEOUT_META = {
  blocked_landing: true,
  blocked_reason: "driver-unavailable",
  legacy_status: "PAUSED",
  legacy_detail: "driver-unavailable",
  driver_error: "request timed out (ReadTimeout)",
  driver_error_kind: "LLMTransientError",
};

function statusEvent(
  id: string,
  status: ConversationState["execution_status"],
  opts: { seq?: number; detail?: string | null; meta?: Record<string, unknown> } = {},
): AgentEvent {
  return {
    id,
    kind: "status",
    source: "system",
    status,
    ...(opts.seq != null ? { seq: opts.seq } : {}),
    ...(opts.detail !== undefined ? { detail: opts.detail } : {}),
    ...(opts.meta ? { meta: opts.meta } : {}),
  } as AgentEvent;
}

function stateFrame(
  cid: string,
  status: ConversationState["execution_status"],
  lastSeq: number,
): ConversationState {
  return {
    conversation_id: cid,
    execution_status: status,
    iteration: 3,
    max_iterations: 30,
    last_seq: lastSeq,
    pending_action_id: null,
    pending_plan_id: null,
  };
}

// ---- pure selector -----------------------------------------------------------

describe("deriveDriverOutage (selector)", () => {
  it("exposes the outage from the latest blocked-landing status with an http status", () => {
    const events = [
      statusEvent("s1", "RUNNING", { seq: 1 }),
      statusEvent("s2", "PAUSED", { seq: 8, detail: "driver-unavailable", meta: OUTAGE_META }),
    ];
    expect(deriveDriverOutage(events)).toEqual({
      httpStatus: 429,
      message: "provider opencode-go returned HTTP 429 type=GoUsageLimitError",
      provider: "opencode-go",
      kind: "LLMTransientError",
    });
  });

  it("is null when a newer RUNNING supersedes the landing (banner drops on resume)", () => {
    const events = [
      statusEvent("s2", "PAUSED", { seq: 8, detail: "driver-unavailable", meta: OUTAGE_META }),
      statusEvent("s3", "RUNNING", { seq: 9 }),
    ];
    expect(deriveDriverOutage(events)).toBeNull();
  });

  it("is null without an http status (timeout/network keeps the generic copy)", () => {
    const events = [
      statusEvent("s2", "PAUSED", { seq: 8, detail: "driver-unavailable", meta: TIMEOUT_META }),
    ];
    expect(deriveDriverOutage(events)).toBeNull();
  });

  it("is null for a non-landing status even if stray keys appear", () => {
    const events = [
      statusEvent("s2", "PAUSED", {
        seq: 8,
        meta: { driver_error_http_status: 429 }, // no blocked_landing → not a landing
      }),
    ];
    expect(deriveDriverOutage(events)).toBeNull();
  });
});

describe("deriveRunErrorDetail (selector)", () => {
  it("returns the latest ERROR status detail", () => {
    const events = [
      statusEvent("s1", "RUNNING", { seq: 1 }),
      statusEvent("s2", "ERROR", {
        seq: 2,
        detail: "provider opencode-go returned HTTP 429 type=GoUsageLimitError",
      }),
    ];
    expect(deriveRunErrorDetail(events)).toBe(
      "provider opencode-go returned HTTP 429 type=GoUsageLimitError",
    );
  });

  it("is null when a newer status supersedes the ERROR", () => {
    const events = [
      statusEvent("s2", "ERROR", { seq: 2, detail: "boom" }),
      statusEvent("s3", "RUNNING", { seq: 3 }),
    ];
    expect(deriveRunErrorDetail(events)).toBeNull();
  });

  it("is null for an ERROR without detail (generic fallback stays)", () => {
    expect(deriveRunErrorDetail([statusEvent("s2", "ERROR", { seq: 2 })])).toBeNull();
  });
});

// ---- through the hook (live + replay-after-reload) ---------------------------

describe("useBuildStream — driverOutage", () => {
  it("surfaces a live interactive 429 landing and drops it on resume", () => {
    const session = { cid: "c_outage_live", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({ type: "state", state: stateFrame("c_outage_live", "RUNNING", 2) });
      // the landing pair the interactive flavor emits: legacy marker, then the ask gate
      sink!({
        type: "event",
        event: statusEvent("s_marker", "PAUSED", {
          seq: 7,
          detail: "driver-unavailable",
          meta: { ...OUTAGE_META, superseded_by_landing: true },
        }),
      });
      sink!({
        type: "event",
        event: statusEvent("s_ask", "AWAITING_USER_QUESTION", {
          seq: 9,
          detail: "evt_question",
          meta: OUTAGE_META,
        }),
      });
    });

    expect(result.current.status).toBe("AWAITING_USER_QUESTION");
    expect(result.current.driverOutage).toEqual({
      httpStatus: 429,
      message: "provider opencode-go returned HTTP 429 type=GoUsageLimitError",
      provider: "opencode-go",
      kind: "LLMTransientError",
    });

    act(() => {
      // the user answered / resumed → a genuinely new RUNNING supersedes
      sink!({ type: "event", event: statusEvent("s_run", "RUNNING", { seq: 10 }) });
    });
    expect(result.current.driverOutage).toBeNull();
  });

  it("survives a reload: state frame + replayed history still expose the outage", () => {
    const session = { cid: "c_outage_replay", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      // fresh connect to an autonomous run that CONCLUDED at PAUSED
      sink!({ type: "state", state: stateFrame("c_outage_replay", "PAUSED", 12) });
      // replayed history (seq <= frameSeq): must not roll status, but the
      // landing meta must still reach the selector
      sink!({ type: "event", event: statusEvent("h_run", "RUNNING", { seq: 3 }) });
      sink!({
        type: "event",
        event: statusEvent("h_landing", "PAUSED", {
          seq: 12,
          detail: "driver-unavailable",
          meta: OUTAGE_META,
        }),
      });
    });

    expect(result.current.status).toBe("PAUSED");
    expect(result.current.driverOutage).not.toBeNull();
    expect(result.current.driverOutage?.httpStatus).toBe(429);
  });

  it("stays null for a timeout landing (generic text path unchanged)", () => {
    const session = { cid: "c_outage_timeout", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({ type: "state", state: stateFrame("c_outage_timeout", "PAUSED", 12) });
      sink!({
        type: "event",
        event: statusEvent("h_landing", "PAUSED", {
          seq: 12,
          detail: "driver-unavailable",
          meta: TIMEOUT_META,
        }),
      });
    });

    expect(result.current.driverOutage).toBeNull();
  });
});

describe("useBuildStream — ERROR detail reaches `error`", () => {
  it("exposes the replayed StatusEvent(ERROR) detail after a reload", () => {
    const session = { cid: "c_err_replay", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({ type: "state", state: stateFrame("c_err_replay", "ERROR", 5) });
      sink!({
        type: "event",
        event: statusEvent("h_err", "ERROR", {
          seq: 5,
          detail: "provider opencode-go returned HTTP 429 type=GoUsageLimitError",
        }),
      });
    });

    expect(result.current.status).toBe("ERROR");
    expect(result.current.error).toBe(
      "provider opencode-go returned HTTP 429 type=GoUsageLimitError",
    );
  });

  it("exposes a live StatusEvent(ERROR) detail and clears it on a newer RUNNING", () => {
    const session = { cid: "c_err_live", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({ type: "state", state: stateFrame("c_err_live", "RUNNING", 2) });
      sink!({
        type: "event",
        event: statusEvent("s_err", "ERROR", { seq: 6, detail: "run-start preflight failed" }),
      });
    });
    expect(result.current.error).toBe("run-start preflight failed");

    act(() => {
      sink!({ type: "event", event: statusEvent("s_run", "RUNNING", { seq: 7 }) });
    });
    expect(result.current.error).toBeNull();
  });

  it("keeps an ErrorEvent's detail authoritative over the status detail", () => {
    const session = { cid: "c_err_event", task: "build" };
    const { result } = renderHook(() => useBuildStream(session));

    act(() => {
      sink!({ type: "state", state: stateFrame("c_err_event", "RUNNING", 2) });
      sink!({
        type: "event",
        event: {
          id: "e1",
          kind: "error",
          source: "system",
          detail: "the real error event detail",
          seq: 6,
        } as AgentEvent,
      });
      sink!({
        type: "event",
        event: statusEvent("s_err", "ERROR", { seq: 7, detail: "status-level detail" }),
      });
    });

    expect(result.current.error).toBe("the real error event detail");
  });
});
