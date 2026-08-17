import { test, expect } from "@playwright/test";

// Live UI smoke against the REAL stack (no mocks): drive a small build end to end,
// screenshot the journey. Bar: build reaches FINISHED in the UI with the artifact
// visible, no dead states.
test("live build smoke through the real UI", async ({ page }) => {
  test.setTimeout(720_000);
  const shots = "/tmp/claude-1000/-var-home-dylan/aa3c8df1-d803-40e0-89de-d73ae8f27f0e/scratchpad/ui-shots";
  await page.goto("http://localhost:5173/");
  await page.screenshot({ path: `${shots}/01-landing.png`, fullPage: false });

  // Enter the BUILD surface explicitly (top surface tabs: search | build | agent)
  // and PROVE we are on it before typing — the first smoke landed on search.
  const buildTab = page.getByText("build", { exact: true }).first();
  await buildTab.click();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${shots}/01b-build-surface.png` });

  const input = page.locator("textarea, [contenteditable=true], input[type=text]").first();
  await input.click();
  await input.fill("Build a single-page site for 'Nightshift Coffee' with the exact tagline 'Open when you are'. Keep it small.");
  await page.keyboard.press("Enter");
  await page.screenshot({ path: `${shots}/02-submitted.png` });

  // Plan approval gates the build — approve via the STABLE test hook, not brittle
  // rendered text (data-disco-control="approve-plan", PlanPanel.tsx). Autonomous
  // runs may skip the gate, so tolerate absence.
  const approve = page.locator('[data-disco-control="approve-plan"]').first();
  try { await approve.waitFor({ state: "visible", timeout: 300_000 }); await approve.click(); } catch {
    // Autonomous runs may not present a plan gate.
  }
  await page.screenshot({ path: `${shots}/03-running.png` });

  // Build-surface truth via TWO independent oracles, both host-owned:
  //  1) the status bar shows the human label "Finished" (STATUS_LABEL maps the
  //     FINISHED enum → "Finished"; assert what the USER sees, case-insensitive).
  //  2) the DeliverablePanel renders its handoff control (build.open-app /
  //     build.download-artifact) — that panel ONLY mounts when the build finished
  //     with a served artifact, so its presence is the artifact-visible proof.
  await expect(page.getByText(/\bfinished\b/i).first()).toBeVisible({ timeout: 600_000 });
  await expect(
    page.locator('[data-disco-control="build.open-app"], [data-disco-control="build.download-artifact"]').first()
  ).toBeVisible({ timeout: 30_000 });
  await page.screenshot({ path: `${shots}/04-finished.png`, fullPage: true });
});
