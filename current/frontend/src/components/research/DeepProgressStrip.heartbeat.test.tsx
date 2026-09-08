/**
 * The strip's honesty rules, at the component level:
 *
 *   1. The heartbeat shows the newest thing the engine REPORTED, with its age.
 *   2. The activity indicator is driven by that age — not by `status` — and
 *      changes visibly at 20 s and again at 60 s.
 *   3. Nothing spins. A spinner is a claim of work; only an event is evidence.
 *   4. With no events, the strip makes no progress claim at all.
 */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { deriveActivity, type DeepActivity, type DeepStats } from "@/lib/deepResearchTrace";
import type { ActionEvent, AgentEvent } from "@/types/agent";
import { DeepProgressStrip } from "./DeepProgressStrip";

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

const EMPTY_STATS: DeepStats = {
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

function renderStrip(events: AgentEvent[], stats: Partial<DeepStats> = {}) {
  const activity: DeepActivity = deriveActivity(events);
  return render(
    <DeepProgressStrip
      brief={null}
      trace={[]}
      stats={{ ...EMPTY_STATS, ...stats }}
      status="RUNNING"
      activity={activity}
      followUpStatus={null}
    />,
  );
}

describe("DeepProgressStrip — the heartbeat is event-driven", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
  });
  afterEach(() => vi.useRealTimers());

  it("shows the turn the engine reported, and the turn counter in the stats row", () => {
    renderStrip(
      [
        action("t1", "turn", { n: 7, of: 16, phase: "searching" }, 0),
        action("s1", "search", { query: "SK On pilot production timeline" }, 0),
      ],
      { searches: 1, sourcesDiscovered: 12 },
    );
    expect(
      screen.getByText('Searching: “SK On pilot production timeline”'),
    ).toBeInTheDocument();
    expect(screen.getByText("Turn 7 of 16 · searching")).toBeInTheDocument();
    // The stats row's progress token is the live budget, not a phase label
    // emitted once before the loop started.
    expect(screen.getByText("Turn 7 of 16")).toBeInTheDocument();
  });

  it("moves live → quiet → silent as the gap grows, with no spinner anywhere", () => {
    const { container } = renderStrip([
      action("t1", "turn", { n: 1, of: 16, phase: "thinking" }, 0),
    ]);

    expect(container.querySelector('[data-dr-signal="live"]')).not.toBeNull();
    expect(screen.getByText("Live")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(20_000));
    expect(container.querySelector('[data-dr-signal="quiet"]')).not.toBeNull();
    expect(screen.getByText("quiet for 20 s")).toBeInTheDocument();

    act(() => vi.advanceTimersByTime(40_000));
    expect(container.querySelector('[data-dr-signal="silent"]')).not.toBeNull();
    expect(screen.getByText("no signal for 1:00")).toBeInTheDocument();

    // The old "⟳ Working" is gone for good.
    expect(container.querySelector(".animate-spin")).toBeNull();
    expect(screen.queryByText("Working")).not.toBeInTheDocument();
  });

  it("attaches a next action once the silence passes a minute", () => {
    renderStrip([action("t1", "turn", { n: 1, of: 16, phase: "thinking" }, 0)]);
    // Under a minute the chip alone carries it — no extra noise.
    act(() => vi.advanceTimersByTime(30_000));
    expect(screen.queryByText(/Nothing reported for/)).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(45_000));
    expect(
      screen.getByText(
        /Nothing reported for 1:15\. The run may still be working — Stop ends it and keeps the sources it has\./,
      ),
    ).toBeInTheDocument();
  });

  it("ticks the age of the newest event while nothing arrives", () => {
    renderStrip([action("s1", "search", { query: "battery pilot lines" }, 0)]);
    act(() => vi.advanceTimersByTime(45_000));
    expect(screen.getByText('Searching: “battery pilot lines” · 45 s')).toBeInTheDocument();
  });

  it("claims no progress at all when no event has reported any", () => {
    const { container } = renderStrip([]);
    expect(container.querySelector("[data-dr-signal]")).toBeNull();
    expect(screen.queryByText(/^Turn /)).not.toBeInTheDocument();
    expect(screen.queryByText(/Searching/)).not.toBeInTheDocument();
    expect(screen.queryByText(/words/)).not.toBeInTheDocument();
    expect(container.querySelector(".animate-spin")).toBeNull();
  });

  it("stays calm while a follow-up answer generates", () => {
    const { container } = render(
      <DeepProgressStrip
        brief={null}
        trace={[]}
        stats={EMPTY_STATS}
        status="RUNNING"
        activity={deriveActivity([action("t1", "turn", { n: 1, of: 8, phase: "thinking" }, 0)])}
        followUpStatus="follow_up"
      />,
    );
    expect(container.querySelector("[data-dr-signal]")).toBeNull();
    expect(screen.queryByText(/^Turn 1 of 8 ·/)).not.toBeInTheDocument();
  });
});
