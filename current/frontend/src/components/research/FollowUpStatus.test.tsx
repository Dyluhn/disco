/**
 * FollowUpStatus — vitest unit tests (WALK-12 / B1).
 *
 * Tests:
 *  (a) Returns null when followUpStatus is null
 *  (b) Returns null when followUpStatus is "follow_up_complete"
 *  (c) Shows "Generating answer…" (default) when in-flight with no phase events
 *  (d) Shows "Reading sources…" when the latest phase event is "reading"
 *  (e) Shows "Writing answer…" when the latest phase event is "synthesizing"
 *  (f) Uses the LAST phase event (ignores earlier ones)
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { FollowUpStatus } from "./FollowUpStatus";
import type { AgentEvent } from "@/types/agent";

// ── Helpers ───────────────────────────────────────────────────────────────────

function makePhaseEvent(phase: string, seq = 1): AgentEvent {
  return {
    id: `phase-${seq}`,
    kind: "action",
    seq,
    source: "agent",
    thought: `phase: ${phase}`,
    tool_call: {
      tool_name: "phase",
      arguments: { phase },
    },
  } as AgentEvent;
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("FollowUpStatus", () => {
  // (a) Hidden when not active
  it("renders nothing when followUpStatus is null", () => {
    const { container } = render(
      <FollowUpStatus followUpStatus={null} events={[]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  // (b) Hidden when complete (answer already rendered in Q&A thread)
  it("renders nothing when followUpStatus is follow_up_complete", () => {
    const { container } = render(
      <FollowUpStatus followUpStatus="follow_up_complete" events={[]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  // (c) Default label when no phase event present
  it("shows 'Generating answer…' when no phase events exist", () => {
    render(<FollowUpStatus followUpStatus="follow_up" events={[]} />);
    expect(screen.getByText("Generating answer…")).toBeInTheDocument();
  });

  // (d) Reading phase
  it("shows 'Reading sources…' for reading phase event", () => {
    render(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[makePhaseEvent("reading", 1)]}
      />,
    );
    expect(screen.getByText("Reading sources…")).toBeInTheDocument();
  });

  // (e) Synthesizing phase
  it("shows 'Writing answer…' for synthesizing phase event", () => {
    render(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[makePhaseEvent("synthesizing", 1)]}
      />,
    );
    expect(screen.getByText("Writing answer…")).toBeInTheDocument();
  });

  // (f) Latest phase event wins
  it("uses the last phase event when multiple exist", () => {
    const events: AgentEvent[] = [
      makePhaseEvent("reading", 1),
      makePhaseEvent("synthesizing", 2),
    ];
    render(<FollowUpStatus followUpStatus="follow_up" events={events} />);
    expect(screen.getByText("Writing answer…")).toBeInTheDocument();
    expect(screen.queryByText("Reading sources…")).not.toBeInTheDocument();
  });

  // (g) Spinner is present when active
  it("renders a spinner element when follow-up is in-flight", () => {
    const { container } = render(
      <FollowUpStatus followUpStatus="follow_up" events={[]} />,
    );
    // The Loader2 icon gets the animate-spin class from Lucide + tailwind
    const spinner = container.querySelector(".animate-spin");
    expect(spinner).not.toBeNull();
  });
});
