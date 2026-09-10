/**
 * Visual evidence for the F2 run-continuity fixes, on a REAL Firefox render of
 * the real components — a passing vitest count is not evidence that a change
 * reached the screen.
 *
 * The story page it drives (`fixtures/f2-continuity-story.html`) is fed only by
 * captured frames; see its header for provenance. Screenshots land in
 * `e2e/_artifacts/f2-continuity/` and are copied into the lane's evidence
 * directory by hand.
 */
import { expect, test } from "@playwright/test";
import * as path from "node:path";

const OUT = path.join("e2e", "_artifacts", "f2-continuity");

test("the run-continuity surfaces say what the run reported", async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 1600 });
  await page.goto("/e2e/fixtures/f2-continuity-story.html");

  const hold = page.locator('[data-f2="hold"]');
  await expect(hold).toContainText("Turn 7 of 16 · 42 sources kept · 3 queries waiting to run");
  const queued = hold.locator('[data-dr-hold-queued="list"] li');
  await expect(queued).toHaveCount(3);
  await expect(queued.nth(2)).toContainText("solid-state separator supplier qualification");
  await hold.screenshot({ path: path.join(OUT, "02-hold-queued-work.png") });

  const writer = page.locator('[data-f2="writer"]');
  await expect(writer).toContainText("Writing the report: thinking, no words yet");
  await expect(writer).toContainText("Research done · 16 turns");
  await expect(writer).not.toContainText("still missing cell cycle life");
  await writer.screenshot({ path: path.join(OUT, "03-writer-leg-no-rationale.png") });

  const checkpoint = page.locator('[data-f2="checkpoint"]');
  await expect(checkpoint.locator("[data-dr-checkpoint]")).toContainText(
    "Research paused before a report was written",
  );
  // The state the user just created is above the trace, not below it.
  const panelBox = (await checkpoint.locator("[data-dr-checkpoint]").boundingBox())!;
  const traceBox = (await checkpoint.getByText("Live trace").boundingBox())!;
  expect(panelBox.y).toBeLessThan(traceBox.y);
  await checkpoint.screenshot({ path: path.join(OUT, "04-checkpoint-above-trace.png") });

  const resumed = page.locator('[data-f2="resumed"]');
  await expect(resumed).toContainText("Turn 1 of 8");
  await expect(resumed.locator("[data-dr-carried]")).toHaveText("9 carried from before Stop");
  await resumed.screenshot({ path: path.join(OUT, "05-resumed-carried-pool.png") });

  const buffered = page.locator('[data-f2="buffered"]');
  await expect(buffered).toContainText(
    "Model producing (this driver does not report progress while it works)",
  );
  await expect(buffered).not.toContainText("Nothing reported for");
  await buffered.screenshot({ path: path.join(OUT, "06-buffered-driver.png") });

  await page.screenshot({ path: path.join(OUT, "00-all.png"), fullPage: true });
});
