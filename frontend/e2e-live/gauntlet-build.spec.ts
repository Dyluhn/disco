import { test, expect } from "@playwright/test";
import * as fs from "node:fs";

/**
 * Close-out gauntlet — Pillar B: one site per primitive through the REAL build
 * surface, then N CLEAN iterations (edit → replan/approve → Finished, no
 * STUCK/PAUSED/dead states). Copy from scratchpad into e2e-live/ ONLY between
 * live runs (vite reload kills run state).
 *
 *   B_LABEL   evidence dir name (e.g. site-leadgen)
 *   B_BRIEF   the build prompt (must exercise the target primitive)
 *   B_EDITS   '||'-separated iteration edit messages (count = iterations)
 *   B_BUDGET  initial build wait ms (default 20 min)
 *   B_ITER_BUDGET per-iteration wait ms (default 15 min)
 */

const LABEL = process.env.B_LABEL ?? "site-adhoc";
const BRIEF = process.env.B_BRIEF ?? "";
const EDITS = (process.env.B_EDITS ?? "").split("||").filter(Boolean);
const BUDGET = Number(process.env.B_BUDGET ?? 1_200_000);
const ITER_BUDGET = Number(process.env.B_ITER_BUDGET ?? 900_000);

const EVID = `/var/home/dylan/projects/disclaude/test-record/gauntlet/${LABEL}`;

const FINISHED = (page: import("@playwright/test").Page) =>
  page.getByText(/\bfinished\b/i).first();
const DELIVERABLE = (page: import("@playwright/test").Page) =>
  page
    .locator(
      '[data-disco-control="build.open-app"], [data-disco-control="build.download-artifact"]',
    )
    .first();

async function approveIfGated(page: import("@playwright/test").Page, timeout: number) {
  const approve = page.locator('[data-disco-control="approve-plan"]').first();
  try {
    await approve.waitFor({ state: "visible", timeout });
    await approve.click();
    return true;
  } catch {
    return false;
  }
}

function assertNoDeadState(body: string, where: string) {
  expect(body, `${where}: STUCK state`).not.toMatch(/\bSTUCK\b/i);
  expect(body, `${where}: needs-input dead state`).not.toMatch(
    /waiting for your (answer|decision)/i,
  );
}

test(`gauntlet BUILD ${LABEL}: build finishes, then ${EDITS.length} clean iterations`, async ({ page }) => {
  test.setTimeout(BUDGET + EDITS.length * ITER_BUDGET + 600_000);
  expect(BRIEF, "B_BRIEF env is required").toBeTruthy();
  fs.mkdirSync(EVID, { recursive: true });
  const shot = (n: string) => page.screenshot({ path: `${EVID}/${n}.png`, fullPage: true });

  await page.goto("/");
  await page.getByText("build", { exact: true }).first().click();
  await page.waitForTimeout(1200);
  await shot("01-build-surface");

  const input = page
    .locator("textarea, [contenteditable=true], input[type=text]")
    .first();
  await input.click();
  await input.fill(BRIEF);
  await page.keyboard.press("Enter");
  await shot("02-submitted");

  // Fail fast on transport/auth failures.
  await page.waitForTimeout(4_000);
  assertNoDeadState(await page.locator("body").innerText(), "submit");
  expect(await page.locator("body").innerText()).not.toMatch(
    /NetworkError|Failed to fetch|csrf required/i,
  );

  await approveIfGated(page, 300_000);
  await shot("03-running");

  // Initial build done: Finished label + DeliverablePanel (artifact-visible proof).
  await expect(FINISHED(page)).toBeVisible({ timeout: BUDGET });
  await expect(DELIVERABLE(page)).toBeVisible({ timeout: 60_000 });
  assertNoDeadState(await page.locator("body").innerText(), "initial build");
  await shot("04-finished");
  fs.appendFileSync(`${EVID}/RESULT.txt`, `build=finished\nurl=${page.url()}\n`);

  // ---- Iterations: each edit must land CLEANLY (replan→approve→Finished). ----
  for (let i = 0; i < EDITS.length; i++) {
    const n = i + 1;
    const edit = EDITS[i];
    const composer = page
      .locator("textarea, [contenteditable=true], input[type=text]")
      .first();
    await composer.click();
    await composer.fill(edit);
    await page.keyboard.press("Enter");
    await shot(`10-iter${n}-sent`);

    // An edit is revision-intent → replan → approval gate (tolerate autonomous skip).
    await approveIfGated(page, 240_000);

    // Wait for the run to conclude again: Finished visible AND no dead states.
    await expect(FINISHED(page)).toBeVisible({ timeout: ITER_BUDGET });
    // Let the status settle (Finished may briefly remain from the prior run —
    // require the deliverable panel too, which remounts after a re-run).
    await expect(DELIVERABLE(page)).toBeVisible({ timeout: 60_000 });
    const body = await page.locator("body").innerText();
    assertNoDeadState(body, `iteration ${n}`);
    await shot(`11-iter${n}-finished`);
    fs.appendFileSync(`${EVID}/RESULT.txt`, `iter${n}=clean\n`);
  }
});
