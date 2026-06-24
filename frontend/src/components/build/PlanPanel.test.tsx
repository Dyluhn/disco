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
import { describe, expect, it } from "vitest";
import { PlanPanel } from "@/components/build/PlanPanel";
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
});
