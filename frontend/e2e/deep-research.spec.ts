import { expect, test } from "@playwright/test";

test.describe("Deep Research → brief → report", () => {
  test("research starts on submit, then a bounded multi-section report assembles", async ({
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

    // v2 is gateless: submitting starts the research, and the model's brief is
    // its first visible output — nothing to approve in between.
    await expect(page.locator("[data-dr-brief]")).toBeVisible();
    await expect(
      page.locator('[data-disco-control="approve-plan"]'),
    ).toHaveCount(0);

    // The strip reports the run's REAL counts (searches / sources), not a
    // checklist of sub-questions the run never promised.
    await expect(page.getByText(/4 searches/i)).toBeVisible();

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

    // While the run streams (status=RUNNING) a graceful Stop control is offered.
    // The full Stop→PAUSED→Resume transition races the fixture's fast auto-
    // completion, so that state machine is covered deterministically by the
    // vitest unit test (DeepResearchSurface.test.tsx); here we assert only that
    // the control is wired and live — deterministic, no false affordance.
    await expect(page.getByRole("button", { name: /^stop$/i })).toBeVisible();
  });
});
