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

  // Deck completion oracle: the deck surface/editor renders slides. Wait for a
  // slide canvas to exist, then screenshot the rendered deck for visual review.
  const deckReady = page
    .locator('[data-disco-control="deck.export"], [data-deck-slide], .deck-slide, [data-disco-control="deck.download.pptx"]')
    .first();
  await deckReady.waitFor({ state: "visible", timeout: 1_200_000 });
  await page.waitForTimeout(4_000);
  await shot("10-deck-ready");

  // Walk up to 14 slides for the visual-quality record (keys → next slide).
  for (let i = 1; i <= 14; i++) {
    await page.keyboard.press("ArrowRight");
    await page.waitForTimeout(700);
    await page.screenshot({ path: `${EVID}/deck-slide-${String(i).padStart(2, "0")}.png` });
  }
  fs.writeFileSync(`${EVID}/RESULT.txt`, `tier=${TIER}\ntopic=${TOPIC}\nfinished=yes\n`);
});
