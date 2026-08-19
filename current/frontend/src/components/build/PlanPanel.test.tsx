/**
 * PlanPanel — the no-steps plan card must render HONESTLY.
 *
 * Live defect (build mode, MiniMax): a plan with a summary but no real steps used
 * to carry a fake "(the planner returned no concrete steps)" placeholder that
 * rendered as a broken numbered "1." step. The backend now keeps steps empty and
 * `derivePlan` drops any legacy sentinel, so the card shows the summary with NO
 * numbered list at all.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { PlanPanel } from "@/components/build/PlanPanel";
import { defaultPlanPanelExpanded } from "@/components/build/planPanelState";
import type { PlanView } from "@/lib/buildTrace";

const NO_STEPS: PlanView = {
  id: "p1",
  summary: "Move the car spawn onto a clear stretch of road.",
  steps: [],
  revision: 4,
  context: "",
};

const WITH_STEPS: PlanView = {
  ...NO_STEPS,
  steps: [{ title: "Tune the spawn point", detail: null }],
};

describe("PlanPanel — no-steps render", () => {
  it("derives its initial expanded state from the approval/run status", () => {
    expect(defaultPlanPanelExpanded({ gate: true })).toBe(true);
    expect(defaultPlanPanelExpanded({ gate: false, status: "AWAITING_PLAN_APPROVAL" })).toBe(true);
    expect(defaultPlanPanelExpanded({ gate: false, status: "RUNNING" })).toBe(false);
    expect(defaultPlanPanelExpanded({ gate: false, status: "FINISHED" })).toBe(false);
    expect(defaultPlanPanelExpanded({ gate: false })).toBe(false);
  });

  it("shows the summary but renders NO numbered list when there are no steps", () => {
    const { container } = render(<PlanPanel plan={NO_STEPS} status="RUNNING" />);
    expect(screen.getByText(NO_STEPS.summary)).toBeInTheDocument();
    // No broken "1." numbered step — the ordered list is omitted entirely.
    expect(container.querySelector("ol")).toBeNull();
    expect(screen.queryByText(/planner returned no concrete steps/i)).toBeNull();
  });

  it("still renders the numbered list when real steps exist", () => {
    const { container } = render(<PlanPanel plan={WITH_STEPS} status="RUNNING" />);
    expect(container.querySelector("ol")).not.toBeNull();
    expect(screen.getByText("Tune the spawn point")).toBeInTheDocument();
  });

  it("defaults expanded while awaiting approval and collapsed while running", () => {
    const { rerender } = render(<PlanPanel plan={WITH_STEPS} onApprove={() => {}} />);
    // Mobile-first: the title can shrink/truncate instead of forcing an
    // overflow (BUG-fix — the "needs your approval" pill and revision chip
    // used to render on top of this heading at 375/430px). lg+ restores the
    // original fixed-width, non-wrapping label via the lg: variants.
    expect(screen.getByText("Review the plan")).toHaveClass("min-w-0", "shrink", "truncate");
    expect(screen.getByText("Review the plan")).toHaveClass(
      "lg:min-w-fit",
      "lg:shrink-0",
      "lg:whitespace-nowrap",
    );
    expect(screen.getByRole("button", { name: /collapse plan/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByTestId("plan-panel-body")).toHaveAttribute("aria-hidden", "false");

    rerender(<PlanPanel plan={WITH_STEPS} status="RUNNING" />);
    expect(screen.getByRole("button", { name: /expand plan/i })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByTestId("plan-panel-body")).toHaveAttribute("aria-hidden", "true");
  });

  it("toggles and keeps the user's manual choice for the component session", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<PlanPanel plan={WITH_STEPS} status="RUNNING" />);

    const toggle = screen.getByRole("button", { name: /expand plan/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);
    expect(screen.getByRole("button", { name: /collapse plan/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByTestId("plan-panel-body")).toHaveAttribute("aria-hidden", "false");

    rerender(<PlanPanel plan={WITH_STEPS} status="FINISHED" />);
    expect(screen.getByRole("button", { name: /collapse plan/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });
});
