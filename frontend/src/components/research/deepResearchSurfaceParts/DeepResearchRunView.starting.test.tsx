/**
 * The start-up state used to be a spinner with no escape: identical after three
 * seconds and after three minutes, and claiming work nobody had reported. It
 * now ages off the moment this client opened the stream and says exactly that.
 */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { useDeepResearch } from "@/hooks/useDeepResearch";
import { deriveActivity, type DeepStats } from "@/lib/deepResearchTrace";
import { DeepResearchRunView } from "./DeepResearchRunView";

const STATS: DeepStats = {
  searches: 0,
  refusals: 0,
  sectionsDone: 0,
  sourcesDiscovered: 0,
  elapsedSeconds: null,
  phase: null,
  activeSection: null,
  lastThought: null,
  lastThoughtLeg: null,
  carriedSources: null,
};

/** A run that has been kicked and has reported nothing yet. */
function runWithNoSignal(): ReturnType<typeof useDeepResearch> {
  return {
    status: "RUNNING",
    error: null,
    brief: null,
    trace: [],
    report: null,
    checkpoint: null,
    stats: STATS,
    activity: deriveActivity([]),
    followUpStatus: null,
    query: "does anything answer yet",
    stop: () => {},
    retry: () => {},
    steer: () => {},
    runExhaustive: () => {},
  } as unknown as ReturnType<typeof useDeepResearch>;
}

describe("DeepResearchRunView — the start-up state does not pretend", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse("2026-09-02T10:00:00.000Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("starts live and says what it is waiting for, without a spinner", () => {
    const { container } = render(<DeepResearchRunView r={runWithNoSignal()} />);
    expect(container.querySelector('[data-dr-signal="live"]')).not.toBeNull();
    expect(
      screen.getByText(/Starting the research — waiting for the engine's first update\./),
    ).toBeInTheDocument();
    expect(container.querySelector(".animate-spin")).toBeNull();
  });

  it("names the silence, how long it has lasted, and what the user can do", () => {
    const { container } = render(<DeepResearchRunView r={runWithNoSignal()} />);
    act(() => vi.advanceTimersByTime(90_000));
    expect(container.querySelector('[data-dr-signal="silent"]')).not.toBeNull();
    expect(screen.getByText("no signal for 1:30")).toBeInTheDocument();
    expect(
      screen.getByText(/Nothing has arrived from the engine since this run opened\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/Stop ends it and keeps whatever it has\./)).toBeInTheDocument();
  });

  it("shows no hold panel when the engine reported no hold", () => {
    render(<DeepResearchRunView r={runWithNoSignal()} />);
    expect(
      screen.queryByRole("region", { name: /Waiting for search engines/i }),
    ).not.toBeInTheDocument();
    expect(document.querySelector('[data-dr-hold]')).toBeNull();
  });

  it("closes steering when writing starts while keeping the run visible", () => {
    const run = runWithNoSignal();
    run.activity = deriveActivity([{
      id: "writing", kind: "action", seq: 1,
      timestamp: "2026-09-02T10:00:00.000Z", thought: "Writing",
      tool_call: { tool_name: "phase", arguments: { phase: "writing" } },
    }]);
    run.stats = { ...STATS, phase: "writing" };
    render(<DeepResearchRunView r={run} />);
    expect(screen.queryByRole("textbox", { name: "Steer the research" })).not.toBeInTheDocument();
    expect(screen.getByText(/You can send a follow-up when the report is ready/)).toBeInTheDocument();
  });
});


function activityEvent(name: string, args: Record<string, unknown>, seq = 1) {
  return {
    id: `activity-${seq}`, kind: "action" as const, seq,
    timestamp: "2026-09-02T10:00:00.000Z", thought: "",
    tool_call: { tool_name: name, arguments: args },
  };
}

describe("DeepResearchRunView — activity before the first brief", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse("2026-09-02T10:00:00.000Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("renders the actual first research-turn heartbeat before a brief or trace exists", () => {
    const run = runWithNoSignal();
    const turn = activityEvent("turn", { n: 1, of: 8, phase: "thinking" });
    run.activity = deriveActivity([turn, activityEvent("model_activity", {
      stage: "research_turn", tokens_streamed: 689, reasoning_tokens: 689,
      seconds: 47.6, call_ordinal: 1,
    }, 2)]);
    const { container, rerender } = render(<DeepResearchRunView r={run} />);
    expect(screen.getByText("Turn 1 of 8 · thinking 48 s")).toBeInTheDocument();
    expect(container.querySelector("[data-dr-starting]")).toBeNull();
    expect(run.brief).toBeNull();
    expect(run.trace).toEqual([]);

    run.activity = deriveActivity([turn, activityEvent("model_activity", {
      stage: "research_turn", tokens_streamed: 856, reasoning_tokens: 856,
      seconds: 79.9, call_ordinal: 1,
    }, 3)]);
    rerender(<DeepResearchRunView r={{ ...run }} />);
    expect(screen.getByText("Turn 1 of 8 · thinking 1:20")).toBeInTheDocument();
    expect(screen.queryByText(/waiting for the engine's first update/)).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(90_000));
    expect(screen.getByText("no signal for 1:30")).toBeInTheDocument();
  });

  it("shows a reported waiting model call even without a turn event", () => {
    const run = runWithNoSignal();
    run.activity = deriveActivity([activityEvent("model_activity", {
      stage: "research_turn", tokens_streamed: 0, seconds: 0,
      call_ordinal: 1, state: "waiting",
    })]);
    const { container } = render(<DeepResearchRunView r={run} />);
    expect(screen.getByText(/Waiting for first model output/)).toBeInTheDocument();
    expect(container.querySelector("[data-dr-starting]")).toBeNull();
  });

  it("does not mistake a status timestamp for reported research activity", () => {
    const run = runWithNoSignal();
    run.activity = deriveActivity([{
      id: "status", kind: "status", seq: 1, status: "RUNNING",
      timestamp: "2026-09-02T10:00:00.000Z",
    }]);
    const { container } = render(<DeepResearchRunView r={run} />);
    expect(container.querySelector("[data-dr-starting]")).not.toBeNull();
    expect(screen.queryByText(/Turn 1 of/)).not.toBeInTheDocument();
  });
});
