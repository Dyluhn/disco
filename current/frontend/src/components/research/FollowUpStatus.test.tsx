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

function makePhaseEvent(
  phase: string,
  seq = 1,
  extra: Record<string, unknown> = {},
): AgentEvent {
  return {
    id: `phase-${seq}`,
    kind: "action",
    seq,
    source: "agent",
    thought: `phase: ${phase}`,
    tool_call: {
      tool_name: "phase",
      arguments: { phase, ...extra },
    },
  } as AgentEvent;
}

/** The grounding pass as lane P1's contract emits it: one `phase` action per
 *  claim, `done <= total`, the last one `done === total`. */
function grounding(done: number, total: number, seq: number): AgentEvent {
  return makePhaseEvent("grounding", seq, { done, total });
}

function makeActivityEvent(
  args: Record<string, unknown>,
  seq: number,
): AgentEvent {
  return {
    id: `activity-${seq}`,
    kind: "action",
    seq,
    source: "agent",
    thought: "model_activity",
    tool_call: { tool_name: "model_activity", arguments: args },
  } as AgentEvent;
}

/** The three frames lane P3 recorded off the wire on its `after-1` follow-up
 *  run, VERBATIM (`lanes/P3-evidence/after-1/ws-frames.txt`, seq 413-415). */
const P3_FRAMES: Record<string, unknown>[] = [
  { stage: "follow_up", tokens_streamed: 1, seconds: 23.4, call_ordinal: 1 },
  { stage: "follow_up", tokens_streamed: 145, seconds: 28.5, call_ordinal: 1 },
  { stage: "follow_up", tokens_streamed: 220, seconds: 30.9, call_ordinal: 1 },
];

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

  // (h) The grounding pass counts itself down (lane P1's `phase` contract).
  //     It is the longest leg of a follow-up and used to emit nothing at all,
  //     so the strip sat on "Writing answer…" for the whole pass.
  it("counts the grounding pass down through a full follow-up sequence", () => {
    const events: AgentEvent[] = [
      makePhaseEvent("reading", 1),
      makePhaseEvent("synthesizing", 2),
    ];
    const view = render(
      <FollowUpStatus followUpStatus="follow_up" events={events} />,
    );
    expect(screen.getByText("Writing answer…")).toBeInTheDocument();

    view.rerender(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[...events, grounding(1, 3, 3)]}
      />,
    );
    expect(
      screen.getByText("Checking claim 1 of 3 against the sources…"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Writing answer…")).not.toBeInTheDocument();

    view.rerender(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[...events, grounding(1, 3, 3), grounding(3, 3, 5)]}
      />,
    );
    expect(
      screen.getByText("Checking claim 3 of 3 against the sources…"),
    ).toBeInTheDocument();
  });

  // (i) A grounding event that carries no counts still says what is happening.
  it("names the grounding pass without counts when the payload has none", () => {
    render(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[makePhaseEvent("grounding", 1)]}
      />,
    );
    expect(
      screen.getByText("Checking the answer against the sources…"),
    ).toBeInTheDocument();
  });

  // (j) The NEWEST phase wins outright. Before this, an unrecognized newest
  //     phase let an older finished one keep speaking — a grounding pass would
  //     have read as "Writing answer…" for its whole run.
  it("does not let an older phase speak once a newer one has landed", () => {
    render(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[
          makePhaseEvent("synthesizing", 1),
          makePhaseEvent("a_phase_the_ui_has_never_seen", 2),
        ]}
      />,
    );
    expect(screen.getByText("Generating answer…")).toBeInTheDocument();
    expect(screen.queryByText("Writing answer…")).not.toBeInTheDocument();
  });

  // (k) The answer call reports itself while it is still open. Before this the
  //      strip sat on "Writing answer…" for the whole 23-31 s call: the TS
  //      mirror had no `follow_up` stage, so the frames rendered nowhere.
  it("reports the answer call's own tokens and seconds while it is open", () => {
    const events: AgentEvent[] = [makePhaseEvent("synthesizing", 1)];
    const view = render(
      <FollowUpStatus followUpStatus="follow_up" events={events} />,
    );
    expect(screen.getByText("Writing answer…")).toBeInTheDocument();

    view.rerender(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[...events, makeActivityEvent(P3_FRAMES[0], 2)]}
      />,
    );
    expect(screen.getByText("Answering — 1 token so far · 23 s")).toBeInTheDocument();
    expect(screen.queryByText("Writing answer…")).not.toBeInTheDocument();

    view.rerender(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[
          ...events,
          makeActivityEvent(P3_FRAMES[0], 2),
          makeActivityEvent(P3_FRAMES[1], 3),
          makeActivityEvent(P3_FRAMES[2], 4),
        ]}
      />,
    );
    expect(screen.getByText("Answering — 220 tokens so far · 31 s")).toBeInTheDocument();
  });

  // (l) A run-level heartbeat must never speak for the follow-up: the writer's
  //     draft/review calls share the action name and belong to the run.
  it("ignores a model_activity frame from any stage but the follow-up", () => {
    render(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[
          makePhaseEvent("reading", 1),
          makeActivityEvent(
            { stage: "draft", tokens_streamed: 900, seconds: 60, call_ordinal: 2 },
            2,
          ),
        ]}
      />,
    );
    expect(screen.getByText("Reading sources…")).toBeInTheDocument();
    expect(screen.queryByText(/^Answering/)).not.toBeInTheDocument();
  });

  // (m) A buffered transport sends ONE frame at call start with nothing
  //      delivered. "0 tokens so far" would read as a stalled model, so the
  //      line says what the transport is instead.
  it("says the provider does not stream instead of reporting zero tokens", () => {
    render(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[
          makeActivityEvent(
            {
              stage: "follow_up",
              tokens_streamed: 0,
              seconds: 0.2,
              call_ordinal: 1,
              streams: false,
            },
            1,
          ),
        ]}
      />,
    );
    expect(
      screen.getByText("Answering (this provider does not stream) · 0 s"),
    ).toBeInTheDocument();
  });

  // (n) The grounding pass follows the answer call, so its frames must not
  //      outlive it — the newest report wins in both directions.
  it("hands the strip back to the grounding pass when the call closes", () => {
    render(
      <FollowUpStatus
        followUpStatus="follow_up"
        events={[
          makeActivityEvent(P3_FRAMES[2], 1),
          grounding(4, 12, 2),
        ]}
      />,
    );
    expect(
      screen.getByText("Checking claim 4 of 12 against the sources…"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^Answering/)).not.toBeInTheDocument();
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
