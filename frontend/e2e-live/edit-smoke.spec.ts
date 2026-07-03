import { test, expect } from "@playwright/test";

// Live UI proof of P8 click-to-edit against the REAL stack (no mocks): build a small
// site, enter edit mode, Inspect + click the headline, and drive the scoped-edit
// composer. Screenshots at every stage so evidence survives a flaky late step.
test("P8 click-to-edit through the real UI", async ({ page }) => {
  test.setTimeout(720_000);
  const shots = "/tmp/claude-1000/-var-home-dylan/aa3c8df1-d803-40e0-89de-d73ae8f27f0e/scratchpad/ui-shots";
  await page.goto("http://localhost:5173/");

  // BUILD surface + a small build.
  await page.getByText("build", { exact: true }).first().click();
  await page.waitForTimeout(1000);
  const input = page.locator("textarea, [contenteditable=true], input[type=text]").first();
  await input.click();
  await input.fill(
    "Build a small single-file index.html landing page for 'NightOwl Coffee' with one hero <h1> headline, a tagline, and a footer. Keep it small.",
  );
  await page.keyboard.press("Enter");

  // Approve the plan (stable hook), then wait for FINISHED.
  const approve = page.locator('[data-disco-control="approve-plan"]').first();
  try { await approve.waitFor({ state: "visible", timeout: 300_000 }); await approve.click(); } catch {}
  await expect(page.getByText(/\bfinished\b/i).first()).toBeVisible({ timeout: 600_000 });
  await page.screenshot({ path: `${shots}/edit-01-finished.png`, fullPage: true });

  // Open the Preview tab, then toggle Edit mode via the STABLE hook (its accessible
  // name is an aria-label, not "Edit" — use data-disco-control).
  try { await page.getByRole("tab", { name: /preview/i }).click(); } catch {}
  await page.waitForTimeout(1500);
  const editToggle = page.locator('[data-disco-control="build.edit-toggle"]').first();
  await editToggle.click({ timeout: 30_000 });
  await page.waitForTimeout(2500); // iframe swaps to the server preview-edit route

  // The preview-edit route reads the ProjectStore SNAPSHOT, which can lag a few
  // seconds behind the FINISHED status → a brief 404. Wait it out: Refresh the edit
  // iframe until the headline is actually present (up to ~50s).
  const frame = page.frameLocator('iframe[title="Editable preview"]');
  let h1Ready = false;
  for (let i = 0; i < 10 && !h1Ready; i++) {
    try {
      await frame.locator("h1").first().waitFor({ state: "visible", timeout: 5000 });
      h1Ready = true;
    } catch {
      await page.locator('[data-disco-control="build.preview-refresh"]').first().click().catch(() => {});
      await page.waitForTimeout(3000);
    }
  }
  await page.screenshot({ path: `${shots}/edit-02-editmode.png`, fullPage: true });

  // Arm Inspect, then click the headline INSIDE the edit iframe.
  try { await page.getByRole("button", { name: /inspect/i }).first().click({ timeout: 15_000 }); } catch {}
  await page.waitForTimeout(800);
  await frame.locator("h1").first().click({ timeout: 15_000 });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${shots}/edit-03-selected.png`, fullPage: true });

  // The scoped-edit composer: type a change and Apply.
  const composer = page.getByPlaceholder(/Describe the change/i).first();
  await composer.waitFor({ state: "visible", timeout: 15_000 });
  await composer.fill("Change the headline to: Night Shift Roasters");
  await page.screenshot({ path: `${shots}/edit-04-composer.png`, fullPage: true });
  await page.locator('[data-disco-control="build.edit-apply"]').first().click();

  // The edit steers the agent — the run goes RUNNING then FINISHED again.
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${shots}/edit-05-applied.png`, fullPage: true });
  await expect(page.getByText(/\bfinished\b/i).first()).toBeVisible({ timeout: 480_000 });
  await page.screenshot({ path: `${shots}/edit-06-done.png`, fullPage: true });
});
