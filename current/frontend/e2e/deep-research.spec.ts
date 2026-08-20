import { expect, test } from "@playwright/test";

test.describe("Deep Research → plan → report", () => {
  test("plan gate, then a bounded multi-section report assembles", async ({
    page,
  }) => {
    await page.goto("/");

    // Enter Deep Research scope from the research surface.
    await page.getByRole("radio", { name: "Deep Research" }).click();

    const input = page.getByPlaceholder(/ask a research question/i);
    await expect(input).toBeVisible();
    await input.fill(
      "What is the current state of solid-state battery commercialization?",
    );
    await input.press("Enter");

    // Plan-approval gate, then approve the decomposition.
    const approve = page.locator('[data-disco-control="approve-plan"]');
    await expect(approve).toBeVisible();
    await approve.click();

    // The fixture run is bounded (hit the per-subquestion round limit) and
    // surfaces that honestly rather than pretending all 6 sub-questions ran.
    await expect(page.getByText(/3 of 6/i)).toBeVisible();

    // The tiered source panel separates Cited / Reviewed / Discovered.
    await expect(page.getByRole("tab", { name: /cited/i })).toBeVisible();
  });

  test("a running deep-research run offers a Stop control", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "Deep Research" }).click();
    const input = page.getByPlaceholder(/ask a research question/i);
    await input.fill(
      "What is the current state of solid-state battery commercialization?",
    );
    await input.press("Enter");
    await page.locator('[data-disco-control="approve-plan"]').click();

    // While the run streams (status=RUNNING) a graceful Stop control is offered.
    // The full Stop→PAUSED→Resume transition races the fixture's fast auto-
    // completion, so that state machine is covered deterministically by the
    // vitest unit test (DeepResearchSurface.test.tsx); here we assert only that
    // the control is wired and live — deterministic, no false affordance.
    await expect(page.getByRole("button", { name: /^stop$/i })).toBeVisible();
  });
});
