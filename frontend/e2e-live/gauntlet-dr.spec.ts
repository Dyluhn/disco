import { test, expect } from "@playwright/test";
import * as fs from "node:fs";

/**
 * Close-out gauntlet — Pillar A: one live deep-research run through the REAL UI,
 * then the report → slides via ONLY the report-bottom "generate slides" control
 * (dr.build-deck). Parameterized by env so the runner can sweep tier × source:
 *
 *   DR_TIER   quick | standard_deep | exhaustive   (dr.depth-tier dropdown)
 *   DR_LABEL  run label for the evidence dir (e.g. quick-web)
 *   DR_TOPIC  the research question
 *   DR_BUDGET whole-run budget ms (report wait; deck gets +20min)
 *
 * Evidence: test-record/gauntlet/<DR_LABEL>/ (numbered screenshots + notes).
 * Source selection (ddgs/arxiv/news) is Settings-owned state the RUNNER flips
 * via the persisted config BEFORE launching this spec — the spec itself only
 * drives the user-visible flow.
 */

const TIER = process.env.DR_TIER ?? "quick";
const LABEL = process.env.DR_LABEL ?? `adhoc-${TIER}`;
const TOPIC =
  process.env.DR_TOPIC ??
  "What are the practical tradeoffs of small on-device language models for summarization?";
const BUDGET = Number(process.env.DR_BUDGET ?? 1_500_000);

const EVID = `/var/home/dylan/projects/disclaude/test-record/gauntlet/${LABEL}`;

const TIER_MENU_LABEL: Record<string, RegExp> = {
  quick: /quick/i,
  standard_deep: /standard/i,
  exhaustive: /exhaustive/i,
};

test(`gauntlet DR ${LABEL}: report finishes with sections+citations, deck builds from the button`, async ({ page }) => {
  test.setTimeout(BUDGET + 1_800_000);
  fs.mkdirSync(EVID, { recursive: true });
  const shot = (n: string) => page.screenshot({ path: `${EVID}/${n}.png`, fullPage: true });

  await page.goto("/");
  await shot("01-landing");

  // Enter deep research via the scope menu (the product path).
  await page.getByRole("button", { name: /^scope:/i }).click();
  await page.getByRole("menuitem", { name: /deep research/i }).click();
  await shot("02-dr-surface");

  // Pick the depth tier through the real dropdown.
  const tierBtn = page.locator('[data-disco-control="dr.depth-tier"]').first();
  await tierBtn.click();
  await page.getByRole("menuitem", { name: TIER_MENU_LABEL[TIER] }).click();
  await shot("03-tier-set");

  // Optional per-run source pill (the real UI control row: Web/News/arXiv/…).
  const srcPill = process.env.DR_SOURCE_PILL;
  if (srcPill) {
    await page.getByRole("button", { name: new RegExp(`^${srcPill}$`, "i") }).click();
    await shot("03b-source-set");
  }

  const input = page.getByPlaceholder(/ask a research question/i);
  await input.fill(TOPIC);
  await page.keyboard.press("Enter");
  await shot("04-submitted");

  // FAIL FAST on a dead submit: transport/origin/auth failures render inline —
  // don't burn the whole budget waiting for a report that never started.
  await page.waitForTimeout(4_000);
  const early = await page.locator("body").innerText();
  expect(early, "submit failed before the run started").not.toMatch(
    /NetworkError|Failed to fetch|csrf required|auth mint|forbidden|unauthorized/i,
  );

  // Plan approval gates DR — approve when offered (autonomous may skip).
  // Live label is "Approve research plan" (fix-c #1); tolerate older variants
  // and the stable control hook.
  const approve = page
    .locator('[data-disco-control="approve-plan"]')
    .or(page.getByRole("button", { name: /approve (research plan|& build)/i }))
    .first();
  try {
    await approve.waitFor({ state: "visible", timeout: 240_000 });
    await shot("05-plan");

    // DR_REVISE: live revision exercise (the 2026-07-07 STUCK regression path —
    // revision → Phase 1R re-propose → a REVISED plan gate, never STUCK).
    // Set DR_REVISE to a revision instruction to enable for this run.
    const revise = process.env.DR_REVISE;
    if (revise) {
      await page
        .locator('[data-disco-control="revise-plan-open"]')
        .or(page.getByRole("button", { name: /revise/i }))
        .first()
        .click();
      await page.getByRole("textbox").last().fill(revise);
      await page
        .locator('[data-disco-control="revise-plan"]')
        .or(page.getByRole("button", { name: /send revision/i }))
        .first()
        .click();
      await shot("05b-revision-sent");
      // The revised plan gate must come back (NOT a STUCK banner).
      await approve.waitFor({ state: "visible", timeout: 240_000 });
      const bodyNow = await page.locator("body").innerText();
      expect(bodyNow, "revision must re-propose, not STUCK").not.toMatch(/\bSTUCK\b/i);
      await shot("05c-revised-plan");
    }

    await approve.click();
  } catch {
    /* no approval gate offered — autonomous path */
  }
  await shot("06-running");

  // Report completion oracle: the Cited tab mounts only with a finished, cited
  // report. Budgeted by tier via DR_BUDGET.
  await expect(page.getByRole("tab", { name: /cited/i })).toBeVisible({ timeout: BUDGET });
  await shot("07-report-done");

  // Honest-content probes: no unresolved section placeholders in the report body.
  const body = await page.locator("body").innerText();
  for (const marker of ["[missing", "MISSING SECTION", "section unavailable", "undefined"]) {
    expect(body.toLowerCase()).not.toContain(marker.toLowerCase());
  }

  // The one allowed interaction for slides: the report-bottom generate control.
  const buildDeck = page.locator('[data-disco-control="dr.build-deck"]').first();
  await buildDeck.scrollIntoViewIfNeeded();
  await shot("08-before-deck");
  await buildDeck.click();
  await shot("09-deck-requested");

  // Deck completion oracle: dr.build-deck HANDS OFF to a new autonomous AGENT
  // conversation (/agent/:cid) that runs slides_generate. Completion = the agent
  // run reaches Finished with a degraded-free deck message; the PPTX artifact is
  // verified off-UI by the runner (converted to per-slide PNGs for review).
  await page.waitForURL(/\/agent\//, { timeout: 60_000 });
  await shot("10-agent-handoff");
  await expect(page.getByText(/\bfinished\b/i).first()).toBeVisible({ timeout: 1_500_000 });
  await shot("11-deck-run-finished");
  const agentBody = await page.locator("body").innerText();
  expect(agentBody, "deck degraded to the plain fallback renderer").not.toMatch(/DEGRADED/i);
  fs.writeFileSync(`${EVID}/RESULT.txt`, `tier=${TIER}\ntopic=${TOPIC}\nfinished=yes\nagentUrl=${page.url()}\n`);
});
