/**
 * Visual evidence for the F1 frontend-truth fixes, on a REAL Firefox render of
 * the real components — a passing vitest count is not evidence that a change
 * reached the screen.
 *
 * The story page it drives (`fixtures/f1-truth-story.html`) is fed only by
 * payloads frozen from the Python producers; see its header for provenance.
 * Screenshots land in `e2e/_artifacts/f1-truth/` and are copied into the lane's
 * evidence directory by hand.
 */
import { expect, test } from "@playwright/test";
import * as path from "node:path";

const OUT = path.join("e2e", "_artifacts", "f1-truth");

test("the frontend-truth surfaces say what the producer reported", async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 1400 });
  await page.goto("/e2e/fixtures/f1-truth-story.html");

  const hold = page.locator('[data-f1="hold"]');
  await expect(hold).toContainText("Turn 7 of 16 · 42 sources kept · 3 queries waiting to run");
  await expect(hold).toContainText("SK On pilot line capacity Georgia 2026");
  await hold.screenshot({ path: path.join(OUT, "01-hold-queued-queries.png") });

  const bounded = page.locator('[data-f1="bounded"]');
  await expect(bounded).toContainText("The run recorded 7 research turns of its 8");
  await expect(bounded).not.toContainText("All 8 research turns");
  await bounded.screenshot({ path: path.join(OUT, "02-bounded-turns-spent.png") });

  // Scoped to the rendered card, not the story's own caption above it.
  const heartbeat = page.locator("[data-f1-heartbeat]");
  await expect(heartbeat).toContainText("Writing the report: thinking, no words yet");
  await expect(heartbeat).toContainText("Research done · 16 turns");
  await expect(heartbeat).not.toContainText("searching");
  await expect(heartbeat).not.toContainText("pilot line capacity in Georgia");
  await page
    .locator('[data-f1="heartbeat"]')
    .screenshot({ path: path.join(OUT, "03-writer-leg-heartbeat.png") });

  const plural = page.locator('[data-f1="plural"]');
  await expect(plural).toContainText("Added 1 source (12 admitted so far)");
  await plural.screenshot({ path: path.join(OUT, "04-pluralisation.png") });

  const failure = page.locator('[data-f1="failure"]');
  await expect(failure).toContainText("The run stopped");
  await expect(failure).toContainText("Search infrastructure");
  for (const label of ["Why", "Where the run stands", "Next", "Still available"]) {
    await expect(failure.getByText(label, { exact: true })).toBeVisible();
  }
  await failure.screenshot({ path: path.join(OUT, "05-error-wall-four-parts.png") });

  await page.screenshot({ path: path.join(OUT, "00-all.png"), fullPage: true });
});
