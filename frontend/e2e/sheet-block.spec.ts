import { expect, test } from "@playwright/test";

/**
 * RP-11 — the `sheet` block renders as an HONEST PREVIEW CARD in the real
 * fixture-mode app: title + filename + sheet badges + "saved to the workspace"
 * provenance/disclaimer, and crucially NO download button (there is no cid on
 * the block render path, so a self-fetching button would always 404 — a false
 * affordance). The "sheet-demo" query routes to the sheet fixture (research.ts).
 */
test.describe("Research → sheet block", () => {
  test("renders an honest preview card with no false-affordance download", async ({ page }) => {
    await page.goto("/");

    const input = page.getByPlaceholder(/ask anything/i);
    await expect(input).toBeVisible();
    await input.fill("sheet-demo");
    await input.press("Enter");

    // The card: title, filename, sheet badges, disclaimer.
    await expect(page.getByText("Q4 Sales Report")).toBeVisible();
    await expect(page.getByText("Revenue")).toBeVisible();
    await expect(page.getByText("Costs")).toBeVisible();
    await expect(page.getByText("Summary")).toBeVisible();
    await expect(page.getByText("3 sheets")).toBeVisible();
    await expect(page.getByText(/saved to the workspace/i)).toBeVisible();
    await expect(page.getByText(/not evaluated/i)).toBeVisible();

    // The essential assertion: NO download button anywhere on the card.
    await expect(page.getByRole("button", { name: /download/i })).toHaveCount(0);

    // Screenshot the card for the visual-evidence record.
    const card = page.getByText("Q4 Sales Report").locator("xpath=ancestor::div[contains(@class,'rounded-card')][1]");
    await card.screenshot({ path: "e2e/_artifacts/rp-11-sheet-card.png" });
  });
});
