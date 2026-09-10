import { test } from "@playwright/test";

// Focused visual evidence for P8: drive ONLY the edit affordance against an
// ALREADY-FINISHED build (snapshot settled → preview-edit 200), skipping the flaky
// live-build phase. cid comes from the passed env (a settled proof conversation).
const CID = process.env.P8_CID || "";

test("P8 edit composer — visual evidence on a settled build", async ({ page }) => {
  test.setTimeout(180_000);
  const shots = test.info().outputDir;
  await page.goto(`http://localhost:5173/build/${CID}`);
  await page.waitForTimeout(4000);
  await page.screenshot({ path: `${shots}/shot-01-resumed.png`, fullPage: true });

  // Preview tab, then Edit mode via the stable hook.
  try { await page.getByRole("tab", { name: /preview/i }).click(); } catch {
    // Preview may already be selected.
  }
  await page.waitForTimeout(1500);
  await page.locator('[data-disco-control="build.edit-toggle"]').first().click({ timeout: 30_000 });
  await page.waitForTimeout(2500);

  // Wait for the edit iframe to actually render the headline (Refresh to retry).
  const frame = page.frameLocator('iframe[title="Editable preview"]');
  let ready = false;
  for (let i = 0; i < 8 && !ready; i++) {
    try { await frame.locator("h1").first().waitFor({ state: "visible", timeout: 5000 }); ready = true; }
    catch {
      await page.locator('[data-disco-control="build.preview-refresh"]').first().click().catch(() => {});
      await page.waitForTimeout(3000);
    }
  }
  await page.screenshot({ path: `${shots}/shot-02-editmode.png`, fullPage: true });

  // Arm Inspect + click the headline → the scoped-edit composer appears.
  // dispatchEvent bypasses Playwright's actionability/stability checks (which can't
  // resolve a stable click point inside the origin-null sandboxed edit iframe) while
  // still firing the selection agent's capture-phase click listener.
  await page.getByRole("button", { name: /inspect/i }).first().click({ timeout: 15_000 });
  await page.waitForTimeout(1000);
  await frame.locator("h1").first().dispatchEvent("click");
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${shots}/shot-03-selected.png`, fullPage: true });

  // Type a change into the composer (the visual proof).
  const composer = page.getByPlaceholder(/Describe the change/i).first();
  await composer.waitFor({ state: "visible", timeout: 15_000 });
  await composer.fill("make the headline larger and orange");
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${shots}/shot-04-composer.png`, fullPage: true });
});
