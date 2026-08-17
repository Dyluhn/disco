/**
 * WO-C3 §7.12 — frozen Firefox proof: the `not_web` SelfHostPanel state, in every
 * UI mounting mode (Build surface + Agent surface).
 *
 * FROZEN acceptance path (plan §1.1: `current/frontend/e2e/export-track1-closeout/**`).
 * Firefox-only (playwright.config.ts) — headless Chromium can't rasterize text/oklch
 * on this host, so visual evidence must come from Firefox.
 *
 * Reachability (offline/fixture mode): a fresh finished build uses the offline
 * conversation id `conv_fixture`, whose release verdict resolves to the default
 * `not_web` shape (`getProjectRelease` → `fixtureRelease` → no web entrypoint). The
 * SelfHostPanel mounts once the run reaches FINISHED (`b.status === "FINISHED" &&
 * release.data`). Build and Agent share the SAME machinery (BuildSurface with a
 * framing), so driving a fresh build on each surface exercises both mounting modes.
 *
 * This is the RUNNABLE state today — it drives the real app to FINISHED and asserts
 * the exact SelfHostPanel action set, then screenshots the panel with its honest
 * reason visible WITHOUT interaction. It is a GREEN-PRESERVATION proof (the baseline
 * `not_web` branch is already honest); it pins that the C3/C6 honesty fix keeps it so.
 */

import { expect, test, type Page } from "@playwright/test";

/** Drive a fresh build-like run to FINISHED so the capability-driven SelfHostPanel
 * mounts. `radioName` selects the surface; `placeholder` is that framing's composer
 * copy. The offline fizzbuzz fixture: submit → approve the plan → confirm the risky
 * step → FINISHED. */
async function driveFreshRunToFinished(
  page: Page,
  radioName: "build" | "agent",
  placeholder: RegExp,
): Promise<void> {
  await page.goto("/");
  await page.getByRole("radio", { name: radioName }).click();
  const input = page.getByPlaceholder(placeholder);
  await expect(input).toBeVisible();
  await input.fill("Write a fizzbuzz script, run it, then delete it to clean up.");
  await input.press("Enter");
  // Plan-approval gate → approve.
  await page.getByRole("button", { name: /approve & build/i }).click();
  // Per-action confirmation gate (the risky `rm`) → confirm; the run then FINISHES.
  await page.getByRole("button", { name: /approve & run/i }).click();
}

/** Assert the exact `not_web` action/copy set on the mounted panel and screenshot it. */
async function assertNotWebPanel(page: Page, artifact: string): Promise<void> {
  const panel = page.locator('[data-disco-control="build.self-host"]');
  // The panel mounts only after FINISHED + the release verdict resolves.
  await expect(panel).toBeVisible();
  await expect(panel.locator('[data-self-host-status="not_web"]')).toBeVisible();

  // Honest reason (plain, no false affordance) — visible without interaction.
  await expect(panel.getByText(/no web server entrypoint was detected/i)).toBeVisible();

  // No self-host-ready affordances: no run command, no "Ready" wording.
  await expect(panel.locator('[data-disco-control="build.self-host-command"]')).toHaveCount(0);
  await expect(panel.getByText(/ready to self-host/i)).toHaveCount(0);

  // The one real action — the plain source download — is present.
  await expect(panel.locator('[data-disco-control="build.download-source"]')).toBeVisible();

  // Screenshot: the honest reason is visible with NO interaction.
  await page.screenshot({ path: artifact, fullPage: true });
}

test.describe("WO-C3 §7.12 — SelfHostPanel not_web (frozen Firefox proof)", () => {
  test("Build surface: a finished non-web run shows the honest not_web panel", async ({ page }) => {
    await driveFreshRunToFinished(page, "build", /describe what you want the agent to build or do/i);
    await assertNotWebPanel(page, "e2e/_artifacts/closeout-selfhost-not-web-build.png");
  });

  test("Agent surface: the SAME machinery shows the identical not_web panel", async ({ page }) => {
    await driveFreshRunToFinished(page, "agent", /describe a task for the agent to carry out/i);
    await assertNotWebPanel(page, "e2e/_artifacts/closeout-selfhost-not-web-agent.png");
  });
});
