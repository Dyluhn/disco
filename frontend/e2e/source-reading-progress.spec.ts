import { expect, test } from "@playwright/test";
import * as path from "node:path";

test("captured source-reading events reach the real progress strip", async ({ page }) => {
  await page.goto("/e2e/fixtures/source-reading-story.html");
  await expect(page.locator('[data-reading="waiting"]')).toContainText("Waiting for first model output · part 1 of 1");
  await expect(page.locator('[data-reading="generating"]')).toContainText("receiving notes");
  await page.screenshot({
    path: path.join(process.env.DISCO_TEST_ARTIFACTS_DIR ?? "test-results", "source-reading.png"),
    fullPage: true,
  });
});
