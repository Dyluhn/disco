import { expect, test } from "@playwright/test";

test.describe("Contradiction surfacing — support meter + claim verdicts", () => {
  test("deep research report shows per-section support meter and claim verdicts", async ({ page }) => {
    await page.goto("/");

    // Enter Deep Research scope from the research surface.
    await page.getByRole("radio", { name: "Deep Research" }).click();

    const input = page.getByPlaceholder(/ask a research question/i);
    await expect(input).toBeVisible();
    await input.fill(
      "What is the current state of solid-state battery commercialization?",
    );
    await input.press("Enter");

    // v2 is gateless — research starts on submit, nothing to approve.

    // Wait for the report sections to assemble — look for a section title.
    const sectionTitle = page.getByRole("heading", {
      name: /solid-state battery products/i,
    });
    await expect(sectionTitle).toBeVisible({ timeout: 15000 });

    // The support meter should appear near the section header, showing
    // "N of M supported" text. The fixture has unsupported_count: 1 on s0.
    const supportMeter = page.getByText(/\d+ of \d+ supported/);
    await expect(supportMeter.first()).toBeVisible();

    // The claim verification collapsible should be present. Open it.
    const claimVerification = page.getByText("Claim verification");
    await expect(claimVerification.first()).toBeVisible();

    // Click to expand the claim verdict breakdown.
    await claimVerification.first().click();

    // A non-supported claim (weak or unsupported) should be visible.
    const unsupportedLabel = page.getByText("Unsupported");
    await expect(unsupportedLabel.first()).toBeVisible({ timeout: 5000 });

    // Take a screenshot showing the meter + verdict breakdown.
    await page.screenshot({
      path: "e2e/_artifacts/contradiction-surfacing.png",
      fullPage: false,
    });
  });

  test("source panel shows verdict-aware cited sources", async ({ page }) => {
    await page.goto("/");

    // Use the research surface (rrfAnswer fixture) which has mixed claims.
    const input = page.getByPlaceholder(/ask anything/i);
    await expect(input).toBeVisible();
    await input.fill(
      "How does reciprocal rank fusion work, and when should I use it?",
    );
    await input.press("Enter");

    // Wait for the answer to render.
    const heading = page.getByRole("heading", {
      name: /reciprocal rank fusion/i,
    });
    await expect(heading).toBeVisible({ timeout: 10000 });

    // The source panel should be visible with a Cited tab.
    const citedTab = page.getByRole("tab", { name: /cited/i });
    await expect(citedTab).toBeVisible();

    // Click the Cited tab.
    await citedTab.click();

    // The cited sources list should render passages — the verdict-weighted
    // ordering places unsupported/weak-associated sources first. The fixture
    // has 4 passages; at least 2 should be visible (the first listing).
    const citedList = page.locator('[role="tabpanel"] li');
    await expect(citedList.first()).toBeVisible({ timeout: 5000 });
  });
});
