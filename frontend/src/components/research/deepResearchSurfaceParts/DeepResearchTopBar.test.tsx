/**
 * UI-25: Ask a Follow-Up / Audio Overview / Build a deck were reachable only by
 * scrolling past the whole report and its sources to the "Need More?" card. The
 * top bar offers the same three next to the MD / PDF exports; the card keeps
 * them too.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ReportEvent } from "@/types/agent";
import { DeepResearchTopBar } from "./DeepResearchTopBar";

const report: ReportEvent = {
  id: "report-1",
  kind: "report",
  query: "What shipped?",
  summary: "A release shipped.",
  sections: [],
  passages: [],
  all_hits: [],
  unsupported_count: 0,
  bounded_by: null,
  depth_tier: "quick",
};

function renderTopBar(over: { status?: string; report?: ReportEvent | null } = {}) {
  const requestReportAction = vi.fn();
  const r = {
    query: report.query,
    report: over.report === undefined ? report : over.report,
    status: over.status ?? "FINISHED",
    cid: "cid-1",
    exportPending: null,
    activity: { stopRequested: null },
    followUps: [],
    stop: vi.fn(),
    kill: vi.fn(),
    resume: vi.fn(),
    retry: vi.fn(),
  };
  render(
    <MemoryRouter>
      <DeepResearchTopBar
        r={r as never}
        onStandardSearch={vi.fn()}
        handleNewResearch={vi.fn()}
        exportCaps={{ md: true, pdf: true }}
        doneNotify={{ armed: false, toggle: vi.fn() } as never}
        topBarIncludeOpen={false}
        setTopBarIncludeOpen={vi.fn()}
        handleTopBarExport={vi.fn()}
        handleTopBarIncludeConfirm={vi.fn()}
        requestReportAction={requestReportAction}
      />
    </MemoryRouter>,
  );
  return { requestReportAction };
}

describe("DeepResearchTopBar — report actions", () => {
  it("offers the report's own actions beside the exports once a report exists", () => {
    renderTopBar();
    expect(screen.getByRole("button", { name: /Follow-up/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Audio/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Deck/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^MD$/ })).toBeInTheDocument();
  });

  it("hands the press to the card that owns the action", async () => {
    const user = userEvent.setup();
    const { requestReportAction } = renderTopBar();
    await user.click(screen.getByRole("button", { name: /Audio/i }));
    expect(requestReportAction).toHaveBeenCalledWith("audio");
  });

  it("offers nothing to act on while the run is still working", () => {
    renderTopBar({ status: "RUNNING", report: null });
    expect(screen.queryByRole("button", { name: /Deck/i })).not.toBeInTheDocument();
  });
});
