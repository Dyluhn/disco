/**
 * The heartbeat's contract: every line is the latest event of its kind plus its
 * age, and NOTHING renders for an event that never arrived.
 *
 * Fixtures are built to the L23/L24 event contract (PLAN.md "Event contract for
 * Phase 3"): each action is an ordinary ActionEvent whose tool_name is the
 * action name and whose arguments are the payload. The backend emitters land
 * with Lane A1; these fixtures are the shape both sides build to.
 */
import { describe, expect, it } from "vitest";
import { deriveActivity } from "@/lib/deepResearchTrace";
import {
  formatDuration,
  heartbeatReading,
  signalReading,
  SIGNAL_QUIET_AFTER_S,
  SIGNAL_SILENT_AFTER_S,
} from "@/lib/deepResearchHeartbeat";
import type { ActionEvent, AgentEvent, ObservationEvent } from "@/types/agent";

const T0 = Date.parse("2026-09-02T10:00:00.000Z");

function iso(offsetSeconds: number): string {
  return new Date(T0 + offsetSeconds * 1000).toISOString();
}

let nextId = 0;

function action(
  name: string,
  args: Record<string, unknown>,
  offsetSeconds: number,
): ActionEvent {
  nextId += 1;
  return {
    id: `evt_${nextId}`,
    kind: "action",
    seq: nextId,
    timestamp: iso(offsetSeconds),
    thought: "",
    tool_call: { tool_name: name, arguments: args },
  };
}

function observation(
  structured: Record<string, unknown>,
  offsetSeconds: number,
): ObservationEvent {
  nextId += 1;
  return {
    id: `evt_${nextId}`,
    kind: "observation",
    seq: nextId,
    timestamp: iso(offsetSeconds),
    action_id: "evt_none",
    tool_result: {
      tool_name: "observation",
      success: true,
      content: "",
      structured,
    },
  };
}

function readAt(events: AgentEvent[], offsetSeconds: number) {
  return heartbeatReading(deriveActivity(events), T0 + offsetSeconds * 1000);
}

describe("heartbeat — one line per contract event", () => {
  it("says nothing at all when no event has said anything", () => {
    const reading = readAt([], 0);
    expect(reading).toEqual({ now: null, turn: null, subquestion: null });
  });

  it("renders a turn with the model's own streaming seconds", () => {
    const events = [
      action("turn", { n: 7, of: 16, phase: "thinking" }, 0),
      action(
        "model_activity",
        { stage: "research_turn", tokens_streamed: 1800, seconds: 42, call_ordinal: 3 },
        1,
      ),
    ];
    expect(readAt(events, 2).now).toBe("Turn 7 of 16 · thinking 42 s");
  });

  it("falls back to the turn's own ticking age when model_activity never arrives", () => {
    const events = [action("turn", { n: 7, of: 16, phase: "thinking" }, 0)];
    expect(readAt(events, 42).now).toBe("Turn 7 of 16 · thinking 42 s");
    // The age is CLIENT-side, so it moves while nothing arrives.
    expect(readAt(events, 95).now).toBe("Turn 7 of 16 · thinking 1:35");
  });

  it("ignores a model_activity heartbeat that belongs to an earlier call", () => {
    const events = [
      action(
        "model_activity",
        { stage: "brief", tokens_streamed: 400, seconds: 90, call_ordinal: 1 },
        0,
      ),
      action("turn", { n: 2, of: 16, phase: "planning" }, 30),
    ];
    // 90 s belonged to the brief; the turn is 5 s old.
    expect(readAt(events, 35).now).toBe("Turn 2 of 16 · planning the next searches 5 s");
  });

  it("renders a search with its query and age, and keeps the turn as context", () => {
    const events = [
      action("turn", { n: 7, of: 16, phase: "searching", subquestion: "pilot line capacity" }, 0),
      action("search", { query: "SK On pilot production timeline" }, 4),
    ];
    const reading = readAt(events, 12);
    expect(reading.now).toBe('Searching: “SK On pilot production timeline” · 8 s');
    expect(reading.turn).toBe("Turn 7 of 16 · searching");
    expect(reading.subquestion).toBe("pilot line capacity");
  });

  it("renders the extraction rollup the observation actually carried", () => {
    const events = [
      action("search", { query: "q" }, 0),
      observation(
        {
          added: 6,
          total_for_subq: 12,
          retrieval_trace: { extraction: { attempted: 6, success: 4 } },
        },
        5,
      ),
    ];
    expect(readAt(events, 6).now).toBe("Read 4 of 6 pages — 6 new sources · 1 s");
  });

  it("omits the page counts when the observation carried no extraction rollup", () => {
    const events = [action("search", { query: "q" }, 0), observation({ added: 2 }, 5)];
    const now = readAt(events, 5).now;
    expect(now).toBe("Read the results — 2 new sources");
    expect(now).not.toMatch(/pages/);
  });

  it("renders the writer's streamed word count only when the phase carried one", () => {
    const withWords = [action("phase", { phase: "writing", words_streamed: 2140 }, 0)];
    expect(readAt(withWords, 0).now).toBe("Writing the report: 2,140 words");

    const withoutWords = [action("phase", { phase: "writing" }, 0)];
    const now = readAt(withoutWords, 0).now;
    expect(now).toBe("Writing the report");
    expect(now).not.toMatch(/words/);
  });

  it("renders review / rework / continuation rounds", () => {
    expect(readAt([action("review", { k: 2, of: 3 }, 0)], 0).now).toBe("Review 2 of 3");
    expect(readAt([action("rework", { k: 1, of: 2 }, 0)], 0).now).toBe("Rework 1 of 2");
    expect(readAt([action("continuation", { k: 1, of: 4 }, 0)], 0).now).toBe(
      "Continuation 1 of 4",
    );
  });

  it("lets the NEWEST reported fact own the line", () => {
    const events = [
      action("phase", { phase: "gather" }, 0),
      action("turn", { n: 3, of: 16, phase: "searching" }, 10),
      action("search", { query: "first" }, 20),
      action("search", { query: "second" }, 30),
    ];
    expect(readAt(events, 30).now).toBe('Searching: “second”');
  });

  it("drops a malformed payload instead of half-rendering it", () => {
    // No `of`, and a phase outside the contract's four.
    const events = [action("turn", { n: 7, phase: "vibes" }, 0)];
    expect(readAt(events, 0).now).toBeNull();
  });
});

describe("activity signal — liveness measured, never assumed", () => {
  it("is live under 20 s", () => {
    expect(signalReading(T0, T0 + 19_000, T0)).toMatchObject({
      level: "live",
      label: "Live",
    });
  });

  it("flips to quiet AT 20 s and names the gap", () => {
    const reading = signalReading(T0, T0 + SIGNAL_QUIET_AFTER_S * 1000, T0);
    expect(reading.level).toBe("quiet");
    expect(signalReading(T0, T0 + 35_000, T0).label).toBe("quiet for 35 s");
  });

  it("flips to silent AT 60 s and names the gap", () => {
    const reading = signalReading(T0, T0 + SIGNAL_SILENT_AFTER_S * 1000, T0);
    expect(reading.level).toBe("silent");
    expect(signalReading(T0, T0 + 72_000, T0).label).toBe("no signal for 1:12");
  });

  it("ages off the stream-open time before any event arrives", () => {
    expect(signalReading(null, T0 + 90_000, T0).label).toBe("no signal for 1:30");
  });

  it("never reads a server clock slightly ahead as a future event", () => {
    expect(signalReading(T0 + 5_000, T0, T0)).toMatchObject({ level: "live", ageSeconds: 0 });
  });
});

describe("formatDuration", () => {
  it("stays readable from seconds to months", () => {
    expect(formatDuration(0)).toBe("0 s");
    expect(formatDuration(59)).toBe("59 s");
    expect(formatDuration(60)).toBe("1:00");
    expect(formatDuration(242)).toBe("4:02");
    expect(formatDuration(3600)).toBe("1h 0m");
    expect(formatDuration(86_400 * 89)).toBe("89d");
  });
});
