import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReportEvent } from "@/types/agent";
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
  ts: "2026-08-16T00:00:00Z",
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

function renderReport(followUpStatus: "follow_up" | "follow_up_complete" | null) {
  const state = {
    query: report.query,
    report,
    assembling: [],
    cid: "cid-1",
    sources: { cited: [], reviewed: [], discovered: [] },
    status: "FINISHED",
    followUps: [],
    followUpStatus,
    events: [],
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
