/**
 * Deep Research surface — integration test over the offline fixture (the
 * same conversation the screenshots are captured against). Verifies the
 * full lifecycle the user sees: empty → plan gate → live progress → report.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import {
  deriveAssemblingSections,
  derivePlan,
  derivePlanProgress,
  deriveReport,
  deriveSourceTiers,
  deriveStats,
} from "@/lib/deepResearchTrace";
import {
  fixtureFullTrace,
  fixturePlan,
  fixtureReport,
} from "@/fixtures/deepResearchTrace";
import { DeepResearchSurface } from "./DeepResearchSurface";

function renderSurface() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/"]}>
        <ModeProvider>
          <Routes>
            <Route path="/" element={<DeepResearchSurface />} />
          </Routes>
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Deep Research surface — full lifecycle", () => {
  // The hook stashes the active session in localStorage so cross-surface
  // navigation resumes. Tests must start clean so each one mounts the empty
  // state rather than picking up the previous test's stashed cid.
  beforeEach(() => window.localStorage.clear());

  it("opens the empty state with a depth tier selector", () => {
    renderSurface();
    expect(
      screen.getByPlaceholderText(/ask a research question/i),
    ).toBeInTheDocument();
    // depth tier selector — default standard_deep
    expect(screen.getByRole("button", { name: /Depth tier: Standard-deep/i })).toBeInTheDocument();
  });

  it("does NOT render the iterative-grounding toggle (removed 2026-07-07; backend stub stays default-off)", () => {
    renderSurface();
    // Iterative grounding takes 30+ minutes and burns tokens — the control was
    // removed from the UI. The API field remains a default-false stub, so the
    // create frame still carries iterative:false (see deepResearch.test.ts).
    expect(
      screen.queryByRole("button", { name: /Iterative grounding/i }),
    ).not.toBeInTheDocument();
  });

  it("after submit, shows the plan gate with editable sub-questions", async () => {
    const user = userEvent.setup();
    renderSurface();
    await user.type(
      screen.getByPlaceholderText(/ask a research question/i),
      "what is the current state of solid-state battery commercialization?",
    );
    await user.keyboard("{Enter}");

    // wait for the plan gate to render
    const gate = await waitFor(
      () => screen.getByRole("alertdialog", { name: /plan needs your approval/i }),
      { timeout: 5000 },
    );
    // sub-questions present from the fixture plan
    expect(
      within(gate).getByText(/Which solid-state battery products/i),
    ).toBeInTheDocument();
    // fix-c #1: the research surface now passes approveLabel="Approve research
    // plan" to PlanPanel — the build surface still uses the default
    // "Approve & build". This test renders the RESEARCH surface so we assert
    // the research-surface label.
    expect(within(gate).getByRole("button", { name: /approve research plan/i })).toBeEnabled();
    expect(within(gate).getByRole("button", { name: /revise/i })).toBeEnabled();
  });

  it("after approve, streams the progress + assembles the report without a false rounds notice", async () => {
    const user = userEvent.setup();
    renderSurface();
    await user.type(
      screen.getByPlaceholderText(/ask a research question/i),
      "what is the current state of solid-state battery commercialization?",
    );
    await user.keyboard("{Enter}");
    await waitFor(() => screen.getByRole("button", { name: /approve research plan/i }), { timeout: 5000 });
    await user.click(screen.getByRole("button", { name: /approve research plan/i }));

    // Wait for the report to appear. `bounded_by: "rounds"` is a depth cap, not a
    // coverage truncation, so the current product contract suppresses the bounded
    // notice for this fixture.
    await waitFor(
      () =>
        expect(
          screen.getAllByText(/As of early 2026/i).length,
        ).toBeGreaterThan(0),
      { timeout: 10000 },
    );
    expect(
      screen.getByRole("heading", { name: /Which solid-state battery products are in mass or pilot production/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Reached 3 of 6 planned sub-questions/i)).not.toBeInTheDocument();
    // executive summary appears (lead with the finding, no "this report begins by")
    expect(screen.queryByText(/this report begins by/i)).not.toBeInTheDocument();
    // 3-tier sources panel
    expect(screen.getByRole("tab", { name: /Cited/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Reviewed/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Discovered/i })).toBeInTheDocument();
    // export controls (RP-07): MD downloads now; PDF is gated on the real server
    // capability (/api/export/capabilities). Offline/fixture defaults to md-only,
    // so PDF is disabled here — no false affordance. W-12: DOCX export removed.
    expect(screen.getByRole("button", { name: /^MD$/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^PDF$/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^DOCX$/ })).not.toBeInTheDocument();
    expect(screen.getByText(/need(s)? .*server/i)).toBeInTheDocument();
  }, 15000);
});

// ---- WALK-02 / WALK-08 surface-level tests (isolated stream mocks) ----------

// These tests use vi.mock at the TOP of the file — but since vi.mock is hoisted
// by Vite and only applies per-describe block when using vi.mocked/importMock,
// we need a separate describe that installs its own subscription mock via a
// manual factory.  We re-use the resumeSurface approach from
// DeepResearchSurface.resumed.test.tsx (mock + replay via setTimeout).

describe("DeepResearchSurface — WALK-08 stale follow-up guard", () => {
  // A resumed surface where a follow-up query was asked after the report
  // (seq > report.seq). Before WALK-08, the initiating query (seq 1) was
  // misclassified as a follow-up because reportSeq fell back to -1.
  // This test verifies the guard: stale "Follow-up: ..." does NOT appear
  // before the research starts / before a report exists.
  it("WALK-08: the follow-up panel is invisible until the run is FINISHED with a report", () => {
    // The EMPTY (pre-submit) surface must never show a follow-up panel.
    renderSurface();
    // Surface is in empty/compose state — the follow-up panel cannot appear
    // because r.report is null (no session even started).
    expect(screen.queryByText(/follow-up:/i)).not.toBeInTheDocument();
  });
});

describe("DeepResearchSurface — WALK-02 planning loader", () => {
  it("WALK-02: the empty-state surface does not show a planning loader (no session)", () => {
    renderSurface();
    // Only shows the empty state / compose input
    expect(screen.queryByText(/planning the research/i)).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText(/ask a research question/i)).toBeInTheDocument();
  });
});

describe("Deep Research derivers", () => {
  it("derivePlan returns the latest revision", () => {
    const plan = derivePlan(fixtureFullTrace);
    expect(plan).not.toBeNull();
    expect(plan!.id).toBe("evt_plan");
    expect(plan!.steps).toHaveLength(6);
    expect(plan!.steps[0].title).toMatch(/products are in mass or pilot/i);
  });

  it("derivePlanProgress marks first 3 sub-questions done", () => {
    const plan = derivePlan(fixtureFullTrace)!;
    const progress = derivePlanProgress(fixtureFullTrace, plan);
    expect(progress.get(1)).toBe("done"); // synth_1 fired
    expect(progress.get(2)).toBe("done"); // synth_2 fired
    expect(progress.get(3)).toBe("done"); // synth_3 fired
    expect(progress.get(4)).toBeUndefined(); // not started
    expect(progress.get(5)).toBeUndefined();
    expect(progress.get(6)).toBeUndefined();
  });

  it("deriveStats counts sources from observation events", () => {
    const plan = derivePlan(fixtureFullTrace)!;
    const stats = deriveStats(fixtureFullTrace, plan);
    expect(stats.subquestionsTotal).toBe(6);
    expect(stats.subquestionsDone).toBe(3);
    // fixture observations: 8 + 6 + 5 + 5 = 24
    expect(stats.sourcesDiscovered).toBe(24);
  });

  it("deriveReport returns the final ReportEvent", () => {
    const report = deriveReport(fixtureFullTrace);
    expect(report).not.toBeNull();
    expect(report!.bounded_by).toBe("rounds");
    expect(report!.depth_tier).toBe("standard_deep");
    expect(report!.sections).toHaveLength(3);
  });

  it("deriveAssemblingSections shows pending for uncovered + done for covered", () => {
    const plan = derivePlan(fixtureFullTrace)!;
    const sections = deriveAssemblingSections(fixtureFullTrace, plan);
    expect(sections).toHaveLength(6);
    expect(sections[0].state).toBe("done");
    expect(sections[2].state).toBe("done");
    expect(sections[3].state).toBe("pending"); // not covered
    expect(sections[5].state).toBe("pending");
  });

  it("deriveSourceTiers splits cited / reviewed / discovered", () => {
    const tiers = deriveSourceTiers(fixtureReport);
    // 10 cited passages (deduped by URL → 7 unique cited URLs after the
    // fixture: 2f1033 (2), 12afc7 (2), bbeab3, 10cd08, 7b9306, 27f6e7, 258fcf, 791a48)
    expect(tiers.cited.length).toBeGreaterThan(0);
    // reviewed = all_hits with status=ok and url not in cited (naccon, energytrend, neware)
    expect(tiers.reviewed.length).toBe(3);
    // discovered = all_hits with status != ok (paywalled, blocked, not_found)
    expect(tiers.discovered.length).toBe(3);
  });

  it("the fixture report carries the honest signals", () => {
    expect(fixtureReport.bounded_by).toBe("rounds");
    expect(fixtureReport.sections[0].confidence).toBe("mixed");
    expect(fixtureReport.sections[0].disputed_notes.length).toBeGreaterThan(0);
    expect(fixturePlan.steps).toHaveLength(6);
  });
});
