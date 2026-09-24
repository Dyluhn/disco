import { describe, expect, it } from "vitest";
import { deriveBuildProgress, derivePlanProgress } from "./buildTrace";
import type { AgentEvent } from "@/types/agent";

function plan(revision: number, count = 6): AgentEvent {
  return { kind: "plan", id: "plan-" + revision, revision,
    steps: Array.from({ length: count }, (_, i) => ({ title: "step " + (i + 1) })),
    summary: "Revision " + revision } as AgentEvent;
}
function snapshot(id: string, steps: unknown): AgentEvent {
  return { kind: "action", id, thought: "", tool_call: {
    tool_name: "update_plan_progress", arguments: { steps },
  } } as AgentEvent;
}
const allDone = Array.from({ length: 6 }, (_, i) => ({ index: i + 1, state: "done" }));
function rejected(actionId: string, kind: "agent_error" | "observation"): AgentEvent {
  return kind === "agent_error"
    ? { kind, id: "error-" + actionId, action_id: actionId, error: "invalid progress" } as AgentEvent
    : { kind, id: "result-" + actionId, action_id: actionId,
      tool_result: { tool_name: "update_plan_progress", success: false, content: "rejected" } } as AgentEvent;
}

describe("Rejected plan progress stays out of the checklist", () => {
  it("replays the G41 revision-two malformed snapshot without counting six completed steps", () => {
    // Actual seq407 plan, seq416 snapshot, seq417 validation error. The valid-
    // looking rows of that rejected action must not survive the missing state.
    const malformed = [...allDone, { index: 7, state: "done" }, { index: 8, state: "done" },
      { index: 9, state: "active" }, ...Array.from({ length: 4 }, () => ({ index: 10, state: "pending" })),
      { index: 10, table: "pending" }];
    const events = [plan(1, 7), snapshot("old", allDone),
      { kind: "status", id: "old-finished", status: "FINISHED" } as AgentEvent,
      plan(2), snapshot("seq416", malformed)];
    expect(deriveBuildProgress(events, "RUNNING").size).toBe(0);
    events.push(rejected("seq416", "agent_error"));
    expect(deriveBuildProgress(JSON.parse(JSON.stringify(events)), "RUNNING").size).toBe(0);
  });

  it.each(["agent_error", "observation"] as const)(
    "retains the last valid snapshot after a correlated %s and accepts a later correction", (kind) => {
      const events = [plan(2), snapshot("accepted", [{ index: 1, state: "active" }]),
        snapshot("failed", allDone), rejected("failed", kind)];
      expect([...deriveBuildProgress(events, "RUNNING")]).toEqual([[1, "active"]]);
      events.push(snapshot("corrected", [{ index: 1, state: "done" }, { index: 2, state: "active" }]));
      expect([...deriveBuildProgress(JSON.parse(JSON.stringify(events)), "RUNNING")])
        .toEqual([[1, "done"], [2, "active"]]);
    },
  );

  it.each([
    { label: "null", bad: null },
    { label: "object", bad: {} },
    { label: "array containing null", bad: [null] },
    { label: "array with missing state", bad: [{ index: 2 }] },
    { label: "array with unknown state", bad: [{ index: 2, state: "invented" }] },
  ])(
    "does not let malformed snapshot $label erase prior valid progress", ({ bad }) => {
      const events = [plan(1), snapshot("accepted", [{ index: 1, state: "active" }]),
        snapshot("malformed", bad)];
      expect([...deriveBuildProgress(events, "RUNNING")]).toEqual([[1, "active"]]);
    },
  );

  it.each(["agent_error", "observation"] as const)(
    "incremental progress also ignores its rejected %s while preserving unrelated completion", (kind) => {
      const events = [plan(2), ...["good", "bad"].map((id, i) =>
        ({ kind: "action", id, thought: "", tool_call: { tool_name: "plan_step",
          arguments: { index: i + 1, state: "done" } } }) as AgentEvent),
      rejected("bad", kind)];
      expect([...derivePlanProgress(JSON.parse(JSON.stringify(events)), "RUNNING")]).toEqual([[1, "done"]]);
    },
  );
});

describe("Progress argument validation is atomic", () => {
  it.each([
    { label: "null", index: null },
    { label: "empty string", index: "" },
    { label: "non-numeric string", index: "bad" },
    { label: "exponent string", index: "1e0" },
    { label: "hex string", index: "0x10" },
    { label: "double separator", index: "1__0" },
    { label: "fraction separator", index: "1.0_0" },
    { label: "fractional number", index: 1.5 },
    { label: "array", index: [] },
    { label: "object", index: {} },
    { label: "infinite number", index: Infinity },
  ])(
    "rejects every row in a snapshot containing invalid index $label before a rejection arrives", ({ index }) => {
      const events = [plan(2), snapshot("valid", [{ index: 1, state: "active" }]),
        snapshot("invalid", [{ index: 1, state: "done" }, { index, state: "done" }])];
      expect([...deriveBuildProgress(events, "RUNNING")]).toEqual([[1, "active"]]);
    },
  );

  it.each([1, "1", " 1 ", "1.0", "+01.00", true])(
    "retains valid tool integer coercion %j in both progress paths", (index) => {
      const events = [plan(2), snapshot("valid", [{ index, state: "done" }])];
      expect([...deriveBuildProgress(events, "RUNNING")]).toEqual([[1, "done"]]);
      events.push({ kind: "action", id: "incremental", thought: "",
        tool_call: { tool_name: "plan_step", arguments: { index, state: "done" } } } as AgentEvent);
      expect([...derivePlanProgress(events, "RUNNING")]).toEqual([[1, "done"]]);
    },
  );

  it("accepts integer separators supported by the tool, without coercing array states", () => {
    expect([...deriveBuildProgress([plan(1, 12),
      snapshot("valid", [{ index: "1_0.0", state: "done" }])], "RUNNING")]).toEqual([[10, "done"]]);
    const events = [plan(2), snapshot("invalid", [{ index: 1, state: ["done"] }])];
    expect(deriveBuildProgress(events, "RUNNING").size).toBe(0);
    events.push({ kind: "action", id: "incremental", thought: "",
      tool_call: { tool_name: "plan_step", arguments: { index: 1, state: ["done"] } } } as AgentEvent);
    expect(derivePlanProgress(events, "RUNNING").size).toBe(0);
  });
});
