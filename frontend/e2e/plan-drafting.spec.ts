import { expect, test } from "@playwright/test";

// The front-door "drafting…" skeleton: live, but no plan yet.
test("a 'Drafting a plan…' skeleton shows between submit and the plan gate", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("radio", { name: "build" }).click();
  const input = page.getByPlaceholder(/describe what you want/i);
  await input.fill("Write a fizzbuzz script, run it, then delete it to clean up.");
  await input.press("Enter");

  // The drafting skeleton appears immediately (RUNNING, no plan), before the gate.
  const drafting = page.getByLabel(/drafting a plan/i);
  await expect(drafting).toBeVisible();
  await page.screenshot({ path: "e2e/_artifacts/plan-drafting.png", fullPage: true });
});
