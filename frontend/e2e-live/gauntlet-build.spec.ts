import { test, expect, type Page } from "@playwright/test";
import * as fs from "node:fs";

/**
 * Close-out gauntlet — Pillar B: one site per primitive through the REAL build
 * surface, then N CLEAN iterations (edit → replan/approve → Finished, no
 * STUCK/dead states).
 *
 * v2 (first-run forensics): gates are handled by a POLL LOOP, not fixed
 * windows — a mid-run steer "Works" for an arbitrary time BEFORE its revised
 * plan gate appears, so a one-shot 240s approve wait missed it and the run
 * parked at AWAITING_PLAN_APPROVAL forever. The loop also answers simple
 * question gates (AskPanel) generically, and disambiguates the STALE
 * "Finished" label: an iteration only counts as finished after the run was
 * observed ACTIVE (Working/gate) first — otherwise the prior run's label +
 * DeliverablePanel would false-pass the iteration.
 *
 *   B_LABEL   evidence dir name (e.g. site-leadgen)
 *   B_BRIEF   the build prompt (must exercise the target primitive)
 *   B_EDITS   '||'-separated iteration edit messages (count = iterations)
 *   B_BUDGET  initial build budget ms (default 25 min)
 *   B_ITER_BUDGET per-iteration budget ms (default 15 min)
 */

const LABEL = process.env.B_LABEL ?? "site-adhoc";
const BRIEF = process.env.B_BRIEF ?? "";
const EDITS = (process.env.B_EDITS ?? "").split("||").filter(Boolean);
const BUDGET = Number(process.env.B_BUDGET ?? 1_500_000);
const ITER_BUDGET = Number(process.env.B_ITER_BUDGET ?? 900_000);

const EVID = `/var/home/dylan/projects/disclaude/test-record/gauntlet/${LABEL}`;

const APPROVE = '[data-disco-control="approve-plan"]';
const ANSWER = '[data-disco-control="answer-question"]';
const INTAKE = '[data-disco-control="submit-questions-v2"]';
const DELIVER =
  '[data-disco-control="build.open-app"], [data-disco-control="build.download-artifact"]';

async function isVisible(page: Page, sel: string): Promise<boolean> {
  try {
    return await page.locator(sel).first().isVisible();
  } catch {
    return false;
  }
}

/** Drive the run to a FRESH Finished state, servicing whatever gates appear.
 * `requireActive`: iteration mode — refuse the stale Finished label until the
 * run has been seen ACTIVE (Working status, a gate, or "Reading your message").
 * Returns counters for the evidence log. Throws on budget exhaustion or
 * question-thrash (>2 question gates in one phase). */
async function driveToFinished(
  page: Page,
  budgetMs: number,
  phase: string,
  requireActive: boolean,
): Promise<{ approvals: number; questions: number }> {
  const deadline = Date.now() + budgetMs;
  let sawActive = !requireActive;
  let approvals = 0;
  let questions = 0;

  while (Date.now() < deadline) {
    // Gate 1: plan approval (initial plan or a steer's revised plan).
    if (await isVisible(page, APPROVE)) {
      await page.locator(APPROVE).first().click();
      approvals++;
      sawActive = true;
      await page.waitForTimeout(1_500);
      continue;
    }
    // Gate 2: simple question (AskPanel) — answer generically; the gauntlet's
    // bar is NO DYLAN REQUIRED. More than 2 questions in one phase = thrash.
    if (await isVisible(page, ANSWER)) {
      questions++;
      if (questions > 2) throw new Error(`${phase}: question-thrash (${questions} gates)`);
      const box = page.getByPlaceholder(/type your answer/i).first();
      await box.fill("Use your best judgment and keep it simple — proceed.");
      await page.locator(ANSWER).first().click();
      sawActive = true;
      await page.waitForTimeout(1_500);
      continue;
    }
    // Gate 3: structured pre-plan intake — fill any text inputs, submit.
    if (await isVisible(page, INTAKE)) {
      questions++;
      for (const ta of await page.locator("textarea:visible, input[type=text]:visible").all()) {
        try {
          if (!(await ta.inputValue())) await ta.fill("Your call — keep it simple.");
        } catch {
          /* non-fillable */
        }
      }
      await page.locator(INTAKE).first().click();
      sawActive = true;
      await page.waitForTimeout(1_500);
      continue;
    }

    const body = await page.locator("body").innerText();
    // Dead states fail immediately — that IS the gauntlet's job.
    if (/\bSTUCK\b/i.test(body)) throw new Error(`${phase}: STUCK`);

    if (!sawActive && /\bWorking\b|\bReading your message\b|\bRunning\b/i.test(body)) {
      sawActive = true;
    }
    if (sawActive && /\bfinished\b/i.test(body) && !/\bWorking\b/i.test(body)) {
      // Fresh finish — require the deliverable handoff too.
      await expect(page.locator(DELIVER).first()).toBeVisible({ timeout: 60_000 });
      return { approvals, questions };
    }
    await page.waitForTimeout(3_000);
  }
  throw new Error(`${phase}: budget exhausted without a fresh Finished`);
}

test(`gauntlet BUILD ${LABEL}: build finishes, then ${EDITS.length} clean iterations`, async ({ page }) => {
  test.setTimeout(BUDGET + EDITS.length * ITER_BUDGET + 600_000);
  expect(BRIEF, "B_BRIEF env is required").toBeTruthy();
  fs.mkdirSync(EVID, { recursive: true });
  const shot = (n: string) => page.screenshot({ path: `${EVID}/${n}.png`, fullPage: true });

  await page.goto("/");
  await page.getByText("build", { exact: true }).first().click();
  await page.waitForTimeout(1_200);
  await shot("01-build-surface");

  const input = page.locator("textarea, [contenteditable=true], input[type=text]").first();
  await input.click();
  await input.fill(BRIEF);
  await page.keyboard.press("Enter");
  await shot("02-submitted");

  await page.waitForTimeout(4_000);
  const early = await page.locator("body").innerText();
  expect(early).not.toMatch(/NetworkError|Failed to fetch|csrf required/i);

  const initial = await driveToFinished(page, BUDGET, "initial build", false);
  await shot("04-finished");
  fs.appendFileSync(
    `${EVID}/RESULT.txt`,
    `build=finished approvals=${initial.approvals} questions=${initial.questions}\nurl=${page.url()}\n`,
  );

  for (let i = 0; i < EDITS.length; i++) {
    const n = i + 1;
    const composer = page.locator("textarea:visible, [contenteditable=true]:visible").last();
    await composer.click();
    await composer.fill(EDITS[i]);
    await page.keyboard.press("Enter");
    await shot(`10-iter${n}-sent`);

    const r = await driveToFinished(page, ITER_BUDGET, `iteration ${n}`, true);
    await shot(`11-iter${n}-finished`);
    fs.appendFileSync(
      `${EVID}/RESULT.txt`,
      `iter${n}=clean approvals=${r.approvals} questions=${r.questions}\n`,
    );
  }
});
