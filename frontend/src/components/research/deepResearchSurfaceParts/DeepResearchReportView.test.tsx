import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent, MessageEvent, ReportEvent } from "@/types/agent";
import capture from "@/lib/__fixtures__/follow-up-grounding.json";
import { DeepResearchReportView } from "./DeepResearchReportView";

const { needMoreProps } = vi.hoisted(() => ({
  needMoreProps: vi.fn(),
}));

vi.mock("../DeepReportView", () => ({ DeepReportView: () => null }));
vi.mock("../TieredSourcePanel", () => ({ TieredSourcePanel: () => null }));
vi.mock("../FollowUpStatus", () => ({ FollowUpStatus: () => null }));
vi.mock("../NeedMoreCard", () => ({
  NeedMoreCard: (props: { followUpBusy: boolean }) => {
    needMoreProps(props);
    return <div data-testid="need-more-card" />;
  },
}));

const report: ReportEvent = {
  id: "report-1",
  seq: 3,
  timestamp: "2026-08-16T00:00:00Z",
  kind: "report",
  source: "agent",
  query: "What shipped?",
  summary: "A release shipped.",
  sections: [],
  passages: [],
  all_hits: [],
  unsupported_count: 0,
  bounded_by: null,
  depth_tier: "quick",
};

function renderReport(
  followUpStatus: "follow_up" | "follow_up_complete" | null,
  thread: { followUps?: MessageEvent[]; events?: AgentEvent[] } = {},
) {
  const state = {
    query: report.query,
    report,
    assembling: [],
    cid: "cid-1",
    sources: { cited: [], reviewed: [], discovered: [] },
    status: "FINISHED",
    followUps: thread.followUps ?? [],
    followUpStatus,
    events: thread.events ?? [],
    followUp: vi.fn(),
  };
  return render(
    <MemoryRouter>
      <DeepResearchReportView r={state as never} />
    </MemoryRouter>,
  );
}

describe("DeepResearchReportView", () => {
  beforeEach(() => needMoreProps.mockClear());

  it("locks follow-up submission for exactly the active follow-up phase", () => {
    const view = renderReport("follow_up");
    expect(screen.getByTestId("need-more-card")).toBeInTheDocument();
    expect(needMoreProps).toHaveBeenLastCalledWith(
      expect.objectContaining({ followUpBusy: true }),
    );

    view.unmount();
    renderReport("follow_up_complete");
    expect(needMoreProps).toHaveBeenLastCalledWith(
      expect.objectContaining({ followUpBusy: false }),
    );
  });
});

// ---- F5 item 1: the follow-up grounding ledger reaches the screen -------------
//
// `follow-up-grounding.json` is the verbatim ActionEvent + assistant
// MessageEvent pair the agent-server appended for two real follow-ups (see the
// fixture's own header). `computeFollowUpGrounding` has read it since F3 and
// nothing rendered it, so an answer and a wall looked the same on screen.

const answered = capture.answered as unknown as AgentEvent[];
const refused = capture.refused as unknown as AgentEvent[];
const answeredMessage = answered[1] as MessageEvent;
const refusedMessage = refused[1] as MessageEvent;

describe("DeepResearchReportView — follow-up grounding ledger", () => {
  it("shows the server's tally under an answer, in words", () => {
    renderReport("follow_up_complete", {
      followUps: [answeredMessage],
      events: answered,
    });
    // The captured ledger is {statements: 11, supported: 10, weak: 1, removed: 0}.
    expect(screen.getByText("10 supported · 1 partly supported · 0 removed")).toBeInTheDocument();
    expect(document.querySelector('[data-dr-followup="answer"]')).toBeInTheDocument();
  });

  it("draws a refused follow-up as a wall, with the server's text unchanged", () => {
    renderReport("follow_up_complete", {
      followUps: [refusedMessage],
      events: refused,
    });
    expect(document.querySelector('[data-dr-followup="wall"]')).toBeInTheDocument();
    expect(screen.queryByText(/supported ·/)).not.toBeInTheDocument();
    // The wall's four parts are the server's own sentences, rendered verbatim.
    expect(screen.getByText(/nothing to check against the report's sources/)).toBeInTheDocument();
  });

  it("gives each answer in a thread its own ledger, not the newest one", () => {
    // The same two captured pairs as one thread: the refusal came second, so
    // the answer above it must still show its own tally.
    const later = [
      { ...refused[0], seq: 354 },
      { ...refused[1], seq: 355 },
    ] as AgentEvent[];
    renderReport("follow_up_complete", {
      followUps: [answeredMessage, later[1] as MessageEvent],
      events: [...answered, ...later],
    });
    expect(screen.getByText("10 supported · 1 partly supported · 0 removed")).toBeInTheDocument();
    expect(document.querySelectorAll('[data-dr-followup="answer"]')).toHaveLength(1);
    expect(document.querySelectorAll('[data-dr-followup="wall"]')).toHaveLength(1);
  });
});
