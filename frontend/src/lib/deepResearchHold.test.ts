/**
 * Hold copy: a wall the system owns has to say why it is up, what state the run
 * is in now, what happens next unattended, and what is still allowed. These
 * tests pin all four — and pin that a hold is never worded as a failure.
 *
 * Also covers the derivation: one hold EPISODE keeps its identity across the
 * re-synced `hold` events the loop emits, and `hold_resumed` clears it.
 */
import { describe, expect, it } from "vitest";
import { holdCopy, holdResumedLabel } from "@/lib/deepResearchHold";
import { deriveActivity, deriveLiveTrace } from "@/lib/deepResearchTrace";
import type { ActionEvent, AgentEvent } from "@/types/agent";

const T0 = Date.parse("2026-09-02T10:00:00.000Z");

function iso(offsetSeconds: number): string {
  return new Date(T0 + offsetSeconds * 1000).toISOString();
}

function action(
  id: string,
  name: string,
  args: Record<string, unknown>,
  offsetSeconds: number,
): ActionEvent {
  return {
    id,
    kind: "action",
    seq: 1,
    timestamp: iso(offsetSeconds),
    thought: "",
    tool_call: { tool_name: name, arguments: args },
  };
}

function holdEvent(id: string, resumeInSeconds: number, offsetSeconds = 0): ActionEvent {
  return action(
    id,
    "hold",
    {
      reason: "search_pool_cooling",
      engines: [
        { name: "brave", resume_at: iso(resumeInSeconds) },
        { name: "mojeek", resume_at: iso(resumeInSeconds + 30) },
      ],
      resume_at: iso(resumeInSeconds),
      sources_retained: 42,
      turn: { n: 7, of: 16 },
      // The producer sends the QUERIES, not a count — see
      // `lib/__fixtures__/hold-action.json` for a verbatim `emit_hold` payload.
      queued_queries: ["pilot line capacity", "cycle life field data", "separator supply"],
    },
    offsetSeconds,
  );
}

function activeHold(events: AgentEvent[]) {
  const hold = deriveActivity(events).hold;
  if (!hold) throw new Error("expected an active hold");
  return hold;
}

describe("hold copy — the four things a wall owes the user", () => {
  it("names the engines, the state, the countdown, and what stopping keeps", () => {
    const copy = holdCopy(activeHold([holdEvent("h1", 220)]), T0);
    expect(copy.why).toBe("brave and mojeek hit their rate limits and are cooling down.");
    expect(copy.notAnError).toMatch(/not an error/i);
    expect(copy.stateNow).toBe("Turn 7 of 16 · 42 sources kept · 3 queries waiting to run");
    expect(copy.next).toBe("Research resumes on its own in 3:40.");
    expect(copy.allowed).toMatch(/keeps the 42 sources found so far/);
    expect(copy.collapsed).toBe("Waiting for search engines — resumes in 3:40");
  });

  it("never words a hold as a failure", () => {
    const copy = holdCopy(activeHold([holdEvent("h1", 220)]), T0);
    const all = Object.values(copy).join(" ");
    expect(all).not.toMatch(/fail|error occurred|something went wrong|unable/i);
  });

  it("says it is retrying rather than counting past zero", () => {
    const copy = holdCopy(activeHold([holdEvent("h1", 10)]), T0 + 30_000);
    expect(copy.next).toBe("Retrying now — waiting for the engines to answer.");
    expect(copy.collapsed).toBe("Waiting for search engines — retrying now");
  });

  it("admits it when the engines gave no return time", () => {
    const event = action(
      "h1",
      "hold",
      {
        reason: "search_rate_starved",
        engines: [],
        resume_at: "not-a-timestamp",
        sources_retained: 0,
        turn: { n: 2, of: 16 },
        queued_queries: [],
      },
      0,
    );
    const copy = holdCopy(activeHold([event]), T0);
    expect(copy.why).toBe("The search pool is out of request budget for the moment.");
    expect(copy.next).toMatch(/did not say when they will be back/);
    expect(copy.stateNow).toBe("Turn 2 of 16 · 0 sources kept");
  });

  it("uses the singular for one engine and one query", () => {
    const event = action(
      "h1",
      "hold",
      {
        reason: "search_pool_cooling",
        engines: [{ name: "brave", resume_at: iso(60) }],
        resume_at: iso(60),
        sources_retained: 1,
        turn: { n: 1, of: 8 },
        queued_queries: ["separator supply"],
      },
      0,
    );
    const copy = holdCopy(activeHold([event]), T0);
    expect(copy.why).toBe("brave hit its rate limit and is cooling down.");
    expect(copy.stateNow).toBe("Turn 1 of 8 · 1 source kept · 1 query waiting to run");
  });
});

describe("hold episodes", () => {
  it("keeps one identity across re-synced hold events, taking the newest countdown", () => {
    const hold = activeHold([holdEvent("h1", 300, 0), holdEvent("h2", 400, 60)]);
    expect(hold.episodeId).toBe("h1");
    expect(holdCopy(hold, T0 + 60_000).next).toBe("Research resumes on its own in 5:40.");
  });

  it("clears on hold_resumed and records the wait", () => {
    const events = [
      holdEvent("h1", 300, 0),
      action("hold_resumed", "hold_resumed", { waited_s: 242, engines_live: ["brave"] }, 242),
    ];
    const activity = deriveActivity(events);
    expect(activity.hold).toBeNull();
    expect(activity.resumed?.payload).toEqual({ waited_s: 242, engines_live: ["brave"] });
  });

  it("re-opens as a NEW episode after a resume", () => {
    const events = [
      holdEvent("h1", 300, 0),
      action("hold_resumed", "hold_resumed", { waited_s: 242, engines_live: ["brave"] }, 242),
      holdEvent("h2", 900, 600),
    ];
    expect(activeHold(events).episodeId).toBe("h2");
  });
});

describe("hold rows in the permanent trace", () => {
  it("records one row per wait and the resume that ended it", () => {
    const events = [
      holdEvent("h1", 300, 0),
      holdEvent("h2", 400, 60), // a re-sync of the SAME wait
      action("hold_resumed", "hold_resumed", { waited_s: 242, engines_live: ["brave", "mojeek"] }, 242),
    ];
    const trace = deriveLiveTrace(events, "RUNNING");
    expect(trace.map((row) => row.label)).toEqual([
      "Waiting for search engines to cool down",
      "Resumed after 4:02 — brave, mojeek live",
    ]);
    expect(trace[0].detail).toBe(
      "brave, mojeek · 3 queries queued: “pilot line capacity”, " +
        "“cycle life field data”, “separator supply”",
    );
    // The wait is over, so its row stops running even though nothing observed it.
    expect(trace[0].status).toBe("done");
    expect(trace[1].status).toBe("done");
  });

  it("leaves an unresolved wait running", () => {
    const trace = deriveLiveTrace([holdEvent("h1", 300, 0)], "RUNNING");
    expect(trace).toHaveLength(1);
    expect(trace[0].status).toBe("running");
  });

  it("never renders the progress events raw", () => {
    const events = [
      action("t1", "turn", { n: 1, of: 8, phase: "planning" }, 0),
      action("m1", "model_activity", { stage: "draft", tokens_streamed: 10, seconds: 2, call_ordinal: 1 }, 1),
      action("r1", "review", { k: 1, of: 2 }, 2),
    ];
    expect(deriveLiveTrace(events, "RUNNING")).toEqual([]);
  });
});

describe("holdResumedLabel", () => {
  it("drops the engine list when the backend named none", () => {
    expect(holdResumedLabel(242, [])).toBe("Resumed after 4:02");
  });
});
