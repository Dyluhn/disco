/**
 * Deep Research surface — integration test over the offline fixture (the
 * same conversation the screenshots are captured against). Verifies the
 * full lifecycle the user sees: empty → plan gate → live progress → report.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
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
  it("opens the empty state with a depth tier selector", () => {
    renderSurface();
    expect(
      screen.getByPlaceholderText(/ask a research question/i),
    ).toBeInTheDocument();
    // depth tier selector — default standard_deep
    expect(screen.getByRole("button", { name: /Depth tier: Standard-deep/i })).toBeInTheDocument();
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
    expect(within(gate).getByRole("button", { name: /approve & build/i })).toBeEnabled();
    expect(within(gate).getByRole("button", { name: /revise/i })).toBeEnabled();
  });

  it("after approve, streams the progress + assembles the report + shows bounded notice", async () => {
    const user = userEvent.setup();
    renderSurface();
    await user.type(
      screen.getByPlaceholderText(/ask a research question/i),
      "what is the current state of solid-state battery commercialization?",
    );
    await user.keyboard("{Enter}");
    await waitFor(() => screen.getByRole("button", { name: /approve & build/i }), { timeout: 5000 });
    await user.click(screen.getByRole("button", { name: /approve & build/i }));

    // wait for the bounded notice to appear (it only renders after ReportEvent
    // arrives → confirms the report has been emitted + reduced into state).
    await waitFor(
      () =>
        expect(
          screen.getByText(/Reached 3 of 6 planned sub-questions/i),
        ).toBeInTheDocument(),
      { timeout: 10000 },
    );
    // report section header
    expect(
      screen.getByRole("heading", { name: /Which solid-state battery products are in mass or pilot production/i }),
    ).toBeInTheDocument();
    // executive summary appears (lead with the finding, no "this report begins by")
    expect(screen.getAllByText(/As of early 2026/i).length).toBeGreaterThan(0);
    expect(screen.queryByText(/this report begins by/i)).not.toBeInTheDocument();
    // 3-tier sources panel
    expect(screen.getByRole("tab", { name: /Cited/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Reviewed/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Discovered/i })).toBeInTheDocument();
    // export button
    expect(screen.getByRole("button", { name: /export/i })).toBeInTheDocument();
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
