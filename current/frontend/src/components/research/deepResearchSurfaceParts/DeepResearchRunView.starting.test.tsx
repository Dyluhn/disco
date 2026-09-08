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
      tool_call: { id: "phase", tool_name: "phase", arguments: { phase: "writing" } },
    }]);
    run.stats = { ...STATS, phase: "writing" };
    render(<DeepResearchRunView r={run} />);
    expect(screen.queryByRole("textbox", { name: "Steer the research" })).not.toBeInTheDocument();
    expect(screen.getByText(/You can send a follow-up when the report is ready/)).toBeInTheDocument();
  });
});
