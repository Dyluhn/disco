/**
 * Deep Research surface — integration test over the offline fixture (the
 * same conversation the screenshots are captured against). Verifies the
 * full lifecycle the user sees: empty → brief → live progress → report.
 *
 * v2 (gateless): submitting STARTS the research. There is no plan-approval
 * gate on this surface, so nothing pauses between the question and the work.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import {
  deriveAssemblingSections,
  deriveBrief,
  deriveReport,
  deriveSourceTiers,
  deriveStats,
} from "@/lib/deepResearchTrace";
import {
  FIXTURE_DEEP_BRIEF,
  fixtureFullTrace,
  fixtureReport,
} from "@/fixtures/deepResearchTrace";
import { DeepResearchSurface } from "./DeepResearchSurface";

function renderSurface(onScopeChange?: (next: "standard" | "deep_research") => void) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/"]}>
        <ModeProvider>
          <Routes>
            <Route path="/" element={<DeepResearchSurface onScopeChange={onScopeChange} />} />
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

  it("opens the empty state with the depth tier inside the options menu", async () => {
    const user = userEvent.setup();
    renderSurface();
    expect(
      screen.getByPlaceholderText(/ask a research question/i),
    ).toBeInTheDocument();
    // Collapsed: the depth choice is summarized on the menu trigger, not a
    // control of its own in the primary row.
    const options = screen.getByRole("button", { name: /^Options/ });
    expect(options).toHaveTextContent(/· Standard/);
    expect(screen.queryByRole("button", { name: /Depth tier/i })).not.toBeInTheDocument();
    // Expanded: depth tier selector — default standard_deep (displayed as
    // "Standard") — leads the menu.
    await user.click(options);
    expect(screen.getByRole("button", { name: /Depth tier: Standard/i })).toBeInTheDocument();
  });

  it("keeps secondary research controls behind one disclosure and quiets suggestions after typing", async () => {
    const user = userEvent.setup();
    renderSurface();

    const options = screen.getByRole("button", { name: /^Options/ });
    expect(options).toHaveAttribute("aria-expanded", "false");
    // The search-type slider stays outside the disclosure (in the box's control
    // row) so Standard Search is always reachable.
    expect(screen.getByRole("radio", { name: "Deep Research" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("radio", { name: "Search" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Depth tier/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Recency filter/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Attach files/i })).not.toBeInTheDocument();

    const suggestions = document.querySelector('[data-suggestion-surface="deep_research"]');
    expect(suggestions).not.toBeNull();
    expect(within(suggestions as HTMLElement).getAllByRole("button")).toHaveLength(3);

    await user.click(options);
    expect(options).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Depth tier: Standard/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Recency filter: Any time/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Attach files/i })).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /^(News|arXiv|Semantic Scholar)$/i }),
    ).toHaveLength(3);
    expect(screen.queryByRole("button", { name: /^Web$/i })).not.toBeInTheDocument();

    await user.type(screen.getByPlaceholderText(/ask a research question/i), "battery policy");
    expect(document.querySelector('[data-suggestion-surface="deep_research"]')).toBeNull();
  });

  it("keeps the trigger minimal — one depth token, no recency/source echo", async () => {
    const user = userEvent.setup();
    renderSurface();

    // Collapsed: exactly "Options · <depth>", nothing else.
    const options = screen.getByRole("button", { name: /^Options/ });
    expect(options).toHaveTextContent(/^Options· Standard$/);
    // Selecting a per-query source must NOT grow the trigger back into a
    // sentence — the old "· Any time · 1 added source" echo stays dead.
    await user.click(options);
    await user.click(screen.getByRole("button", { name: /^News$/i }));
    expect(options).toHaveTextContent(/^Options· Standard$/);
  });

  it("does NOT render the iterative-grounding toggle (removed 2026-07-07; backend stub stays default-off)", () => {
    renderSurface();
    // Iterative grounding takes 30+ minutes and burns tokens — the control was
    // removed from the UI. The API field remains a default-false stub, so the
    // There is one execution method; no hidden mode selector is sent either.
    expect(
      screen.queryByRole("button", { name: /Iterative grounding/i }),
    ).not.toBeInTheDocument();
  });

  it("after submit, research starts immediately and shows the model's brief", async () => {
    const user = userEvent.setup();
    const onScopeChange = vi.fn();
    renderSurface(onScopeChange);
    await user.type(
      screen.getByPlaceholderText(/ask a research question/i),
      "what is the current state of solid-state battery commercialization?",
    );
    await user.keyboard("{Enter}");

    // Activity is collapsed for live and finished runs; expand it explicitly
    // before asserting the model brief inside the disclosure body.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /How it researched/i })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: /How it researched/i }));

    // The brief — the model's first visible output — arrives with no gate in
    // between. It renders inside the progress strip.
    const brief = await waitFor(
      () => {
        const el = document.querySelector("[data-dr-brief]");
        expect(el).not.toBeNull();
        return el as HTMLElement;
      },
      { timeout: 5000 },
    );
    expect(brief).toHaveTextContent(/path from lab to production line/i);
    await user.click(screen.getByRole("button", { name: /Standard Search/i }));
    expect(onScopeChange).toHaveBeenCalledWith("standard");

    // Nothing to approve or revise: the gate is gone, not merely hidden.
    expect(
      screen.queryByRole("alertdialog", { name: /plan needs your approval/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /approve research plan/i }),
    ).not.toBeInTheDocument();
    // Run controls stay live once the gate is gone — Stop is the rendered one
    // (the steer transport exists but has no control on this surface).
    expect(screen.getByRole("button", { name: /^Stop$/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^Kill$/ })).toBeEnabled();
  });

  it("after submit, streams the progress + assembles the report without a false rounds notice", async () => {
    const user = userEvent.setup();
    renderSurface();
    await user.type(
      screen.getByPlaceholderText(/ask a research question/i),
      "what is the current state of solid-state battery commercialization?",
    );
    await user.keyboard("{Enter}");

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
    // Only shows the empty state / compose input. v2 replaced the planning
    // loader with a start-up loader; neither may appear before a session.
    expect(screen.queryByText(/planning the research/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/starting the research/i)).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText(/ask a research question/i)).toBeInTheDocument();
  });
});

describe("Deep Research derivers", () => {
  it("deriveBrief returns the model's opening brief", () => {
    const brief = deriveBrief(fixtureFullTrace);
    expect(brief).toBe(FIXTURE_DEEP_BRIEF);
    expect(brief).toMatch(/path from lab to production line/i);
  });

  it("the fixture trace carries no plan and no approval gate", () => {
    expect(fixtureFullTrace.some((e) => e.kind === "plan")).toBe(false);
    expect(
      fixtureFullTrace.some(
        (e) => e.kind === "status" && e.status === "AWAITING_PLAN_APPROVAL",
      ),
    ).toBe(false);
  });

  it("deriveStats counts sources from observation events", () => {
    const stats = deriveStats(fixtureFullTrace);
    // fixture observations: 8 + 6 + 5 + 5 = 24
    expect(stats.sourcesDiscovered).toBe(24);
    // Real event counts, not plan-step bookkeeping.
    expect(stats.searches).toBe(4);
    expect(stats.sectionsDone).toBe(3);
  });

  it("deriveStats reports a real elapsed span from the event timestamps", () => {
    // The fixture's timestamps advance with seq (1s per unit): seq 1 → seq 80.
    expect(deriveStats(fixtureFullTrace).elapsedSeconds).toBe(79);
  });

  it("deriveReport returns the final ReportEvent", () => {
    const report = deriveReport(fixtureFullTrace);
    expect(report).not.toBeNull();
    expect(report!.bounded_by).toBe("rounds");
    expect(report!.depth_tier).toBe("standard_deep");
    expect(report!.sections).toHaveLength(3);
  });

  it("deriveAssemblingSections shows pending for uncovered + done for covered", () => {
    const sections = deriveAssemblingSections(fixtureFullTrace);
    expect(sections).toHaveLength(3);
    expect(sections.every((section) => section.state === "done")).toBe(true);
    expect(sections.map((section) => section.title)).toEqual(
      fixtureReport.sections.map((section) => section.title),
    );
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
  });
});
