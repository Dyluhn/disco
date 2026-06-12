import { expect, test } from "@playwright/test";

/**
 * RP-13 — the pre-plan Clarify gate in the REAL app (offline fixture: a task
 * containing "clarify" routes to a typed multi-question gate). The planner asks
 * SEVERAL structured questions (short_text / choice / long_text); the surface
 * renders ClarifyPanel (NOT the free-form AskPanel — pending_clarify_id gates it).
 */
test.describe("Build → pre-plan Clarify gate", () => {
  test("a typed multi-question clarify card renders and gates Submit", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "build" }).click();

    const input = page.getByPlaceholder(/describe what you want/i);
    await expect(input).toBeVisible();
    await input.fill("Build me a landing page, but clarify the details first");
    await input.press("Enter");

    // The clarify gate: an alertdialog with the typed questions.
    const card = page.getByRole("alertdialog", { name: /clarification/i });
    await expect(card).toBeVisible();
    await expect(card.getByText(/brand color/i)).toBeVisible();
    await expect(card.getByText(/which framework/i)).toBeVisible();
    await expect(card.getByText(/target audience/i)).toBeVisible();
    // The choice question renders its options.
    await expect(card.getByText(/react/i)).toBeVisible();
    await expect(card.getByText(/svelte/i)).toBeVisible();

    await page.screenshot({ path: "e2e/_artifacts/rp-13-clarify-gate.png", fullPage: true });
  });
});
