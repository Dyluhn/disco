/**
 * UI-14 — the collapsed Activity section after a run finishes.
 *
 * It used to read "Done · Verbose chat is off — click to expand the full
 * history", which on the finish screen looked like an empty box under the
 * "Event N of N" row. A settled run now gets a one-line receipt instead: how
 * long it took, how many steps it ran, and the expand affordance spelled out.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AgentStageCard } from "@/components/build/AgentStageCard";
import type { ActivityItem } from "@/lib/buildTrace";
import type { AgentEvent } from "@/types/agent";

const stamped = (id: string, timestamp: string): AgentEvent =>
  ({ id, kind: "action", thought: "", tool_call: null, timestamp }) as AgentEvent;

const steps = (n: number): ActivityItem[] =>
  Array.from({ length: n }, (_, i) => ({
    kind: "action",
    id: `s${i}`,
    label: `Wrote file-${i}.html`,
    status: "done",
    attention: false,
  })) as unknown as ActivityItem[];

describe("AgentStageCard — finished summary (UI-14)", () => {
  const events = [
    stamped("e1", "2026-09-09T10:00:00Z"),
    stamped("e2", "2026-09-09T10:17:00Z"),
  ];

  it("summarises duration + step count instead of the verbose-chat line", () => {
    render(<AgentStageCard events={events} status="FINISHED" activity={steps(3)} />);
    expect(screen.getByText("Finished in 17 min · 3 steps · expand history")).toBeInTheDocument();
    expect(screen.queryByText(/verbose chat is off/i)).toBeNull();
  });

  it("the summary is the expand affordance — clicking it reveals the history", () => {
    render(<AgentStageCard events={events} status="FINISHED" activity={steps(3)} />);
    expect(screen.queryByText("Wrote file-0.html")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /expand agent history/i }));
    expect(screen.getByText("Wrote file-0.html")).toBeInTheDocument();
    expect(screen.getByText("Finished in 17 min · 3 steps · collapse history")).toBeInTheDocument();
  });

  it("drops the duration clause when the stream carries no timestamps", () => {
    render(
      <AgentStageCard
        events={[stamped("e1", "")]}
        status="FINISHED"
        activity={steps(1)}
      />,
    );
    expect(screen.getByText("Finished · 1 step · expand history")).toBeInTheDocument();
  });

  it("a live run keeps the verbose-chat line (the summary is terminal-only)", () => {
    render(<AgentStageCard events={events} status="RUNNING" activity={steps(3)} />);
    expect(screen.getByText(/verbose chat is off/i)).toBeInTheDocument();
    expect(screen.queryByText(/expand history/)).toBeNull();
  });
});
