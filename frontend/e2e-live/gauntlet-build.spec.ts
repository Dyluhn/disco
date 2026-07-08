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
 *
 * Freshness oracle (v3): state VERSION, not observed transitions. v2 required
 * WITNESSING an active phase before accepting FINISHED — but the chip is fed by
 * the UI's live stream, and on a page alive 45+ minutes that stream can die
 * silently. Frozen chip → never "saw" active → a genuinely fresh FINISHED
 * refused forever → wedged 3 complete runs (2026-07-08). Now the chip exposes
 * data-seq (highest event seq observed); a FINISHED with seq > startSeq (the
 * seq before we sent the edit) IS fresh, whether or not we watched it happen.
 * A watchdog reload heals dead streams: no seq movement for 3 min → reload;
 * the state frame re-fetch reports current status + last_seq regardless of
 * stream health. Throws on budget exhaustion or question-thrash. */
async function driveToFinished(
  page: Page,
  budgetMs: number,
  phase: string,
  requireActive: boolean,
  startSeq: number,
): Promise<{ approvals: number; questions: number }> {
  const deadline = Date.now() + budgetMs;
  let sawActive = !requireActive;
  let approvals = 0;
  let questions = 0;
  let lastSeq = -1;
  let lastSeqMoveAt = Date.now();

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

    // Read phase + state-version DETERMINISTICALLY from the status chip — NEVER
    // by regexing the page body (agent prose false-matches "working"/"finished").
    // Null until the first state frame lands → keep polling.
    const chip = page.locator('[data-disco-control="build.status"]').first();
    const runStatus = await chip.getAttribute("data-status").catch(() => null);
    const chipSeq = Number((await chip.getAttribute("data-seq").catch(() => null)) ?? "0") || 0;

    // Dead states fail immediately — that IS the gauntlet's job.
    if (runStatus === "STUCK") throw new Error(`${phase}: STUCK`);
    if (runStatus === "ERROR") throw new Error(`${phase}: ERROR`);

    // Watchdog: the chip is stream-fed; a long-lived page's stream can die
    // silently and freeze the chip mid-run. No seq movement for 3 min while the
    // server should be working → reload; the fresh state frame reports current
    // status + last_seq without needing a healthy stream.
    if (chipSeq > lastSeq) {
      lastSeq = chipSeq;
      lastSeqMoveAt = Date.now();
    } else if (Date.now() - lastSeqMoveAt > 180_000) {
      // The reload can race the outer test-timeout teardown; a closed page must
      // surface as THIS phase's budget verdict, not a cryptic page.reload error.
      try {
        await page.reload();
        await page.waitForTimeout(3_000);
      } catch {
        throw new Error(`${phase}: budget exhausted (page closed during watchdog reload)`);
      }
      lastSeqMoveAt = Date.now();
      continue;
    }

    // Witnessing an active/gated phase still counts (fast acceptance)...
    if (
      runStatus === "RUNNING" ||
      runStatus === "PAUSED" ||
      runStatus === "AWAITING_PLAN_APPROVAL" ||
      runStatus === "AWAITING_USER_QUESTION" ||
      runStatus === "AWAITING_USER_DECISION" ||
      runStatus === "WAITING_FOR_CONFIRMATION"
    ) {
      sawActive = true;
    }
    // ...but the AUTHORITATIVE freshness signal is the state version: events
    // exist beyond the seq recorded before this phase's edit was sent.
    const fresh = chipSeq > startSeq;
    if ((sawActive || fresh) && runStatus === "FINISHED") {
      // Fresh finish — require the deliverable handoff too.
      await expect(page.locator(DELIVER).first()).toBeVisible({ timeout: 60_000 });
      return { approvals, questions };
    }
    await page.waitForTimeout(3_000);
  }
  throw new Error(`${phase}: budget exhausted without a fresh Finished`);
}

/** Current state-version from the chip (0 if unmounted/no frame yet). */
async function chipSeqNow(page: Page): Promise<number> {
  const raw = await page
    .locator('[data-disco-control="build.status"]')
    .first()
    .getAttribute("data-seq")
    .catch(() => null);
  return Number(raw ?? "0") || 0;
}

test(`gauntlet BUILD ${LABEL}: build finishes, then ${EDITS.length} clean iterations`, async ({ page }) => {
  test.setTimeout(BUDGET + EDITS.length * ITER_BUDGET + 600_000);
  expect(BRIEF, "B_BRIEF env is required").toBeTruthy();
  fs.mkdirSync(EVID, { recursive: true });
  const shot = (n: string) => page.screenshot({ path: `${EVID}/${n}.png`, fullPage: true });

  // Remote-fleet mode: the packaged deploy can't auto-pair from a remote browser,
  // so the runner pre-mints a session (pairing-token mint via the deploy host) and
  // hands us the cookie. Same-host cookies reach both servers (shared signer).
  const sessionCookie = process.env.LIVE_SESSION_COOKIE ?? "";
  if (sessionCookie) {
    const base = process.env.LIVE_BASE_URL ?? "";
    await page.context().addCookies([
      { name: "disco_session", value: sessionCookie, url: base || "http://localhost" },
    ]);
  }

  await page.goto("/");
  // Force a FRESH build compose. Clicking the build chip alone RESUMES the most
  // recent ACTIVE build conversation (resumeTargetFor → /build/:cid), so a
  // suspended orphan from a prior run hijacks the surface and there is no blank
  // composer to type into. "New" (shell.nav-new) calls markAllModesFresh(),
  // which flips every mode's resume target back to the splash composer — click
  // it BEFORE the build chip so the chip lands on a clean build session.
  await page.locator('[data-disco-control="shell.nav-new"]').first().click();
  await page.waitForTimeout(600);
  await page.locator('[data-disco-control="shell.mode-build"]').first().click();
  await page.waitForTimeout(1_200);
  await shot("01-build-surface");

  // Guard: a fresh compose must not carry a prior run's plan-review / kill UI.
  const pre = await page.locator("body").innerText();
  expect(pre, "expected a fresh build composer, not a resumed run").not.toMatch(
    /Reviewing plan|\bKill\b/i,
  );

  // Autonomous (headless) mode: auto-approves its own plan, won't ask
  // questions, and stops CLEANLY instead of dangling at "what next?". This is
  // the hands-off "build + 5 quick edits, no pause" scenario the gauntlet is
  // meant to prove, and it routes an actionless-after-completed-edit through
  // the synthesize-finish ladder rather than the non-autonomous ask-user path.
  // Default is OFF (fresh browser context) → a single click enables it.
  const autoToggle = page.locator('[data-disco-control="build.autonomous-toggle"]').first();
  if (await autoToggle.isVisible().catch(() => false)) {
    await autoToggle.click();
    await page.waitForTimeout(400);
    await shot("01b-autonomous-on");
  }

  const input = page
    .locator("textarea:visible, [contenteditable=true]:visible, input[type=text]:visible")
    .first();
  await input.click();
  await input.fill(BRIEF);
  await page.keyboard.press("Enter");
  await shot("02-submitted");

  await page.waitForTimeout(4_000);
  const early = await page.locator("body").innerText();
  expect(early).not.toMatch(/NetworkError|Failed to fetch|csrf required/i);

  const initial = await driveToFinished(page, BUDGET, "initial build", false, 0);
  await shot("04-finished");
  fs.appendFileSync(
    `${EVID}/RESULT.txt`,
    `build=finished approvals=${initial.approvals} questions=${initial.questions}\nurl=${page.url()}\n`,
  );

  for (let i = 0; i < EDITS.length; i++) {
    const n = i + 1;
    // Record the state version BEFORE the edit: anything beyond this seq is
    // work caused by (or after) this edit — the freshness baseline.
    const preEditSeq = await chipSeqNow(page);
    const composer = page.locator("textarea:visible, [contenteditable=true]:visible").last();
    await composer.click();
    await composer.fill(EDITS[i]);
    await page.keyboard.press("Enter");
    await shot(`10-iter${n}-sent`);

    const r = await driveToFinished(page, ITER_BUDGET, `iteration ${n}`, true, preEditSeq);
    await shot(`11-iter${n}-finished`);
    fs.appendFileSync(
      `${EVID}/RESULT.txt`,
      `iter${n}=clean approvals=${r.approvals} questions=${r.questions}\n`,
    );
  }
});
