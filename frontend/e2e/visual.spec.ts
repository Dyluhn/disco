import { type Page, expect, test } from "@playwright/test";

/**
 * Visual regression — Firefox baselines for key states in both themes.
 * (Headless chromium can't rasterize text on this box; Firefox can — see the
 * config.) Playwright disables CSS animations during screenshots; the small
 * settle waits let streamed/entrance content land before capture.
 */

async function toggleTheme(page: Page) {
  await page.getByRole("button", { name: /switch to (dark|light) theme/i }).click();
  await page.waitForTimeout(200);
}

test.describe("visual regression", () => {
  test("research answer — both themes", async ({ page }) => {
    await page.goto("/");
    await page
      .getByPlaceholder(/ask anything/i)
      .fill("How does reciprocal rank fusion work, and when should I use it?");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("tab", { name: /cited/i })).toBeVisible();
    await page.waitForTimeout(500); // let the streamed answer settle

    await expect(page).toHaveScreenshot("research-answer.png", { fullPage: true });
    await toggleTheme(page);
    await expect(page).toHaveScreenshot("research-answer-alt-theme.png", {
      fullPage: true,
    });
  });

  test("settings — both themes", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: /^settings$/i })).toBeVisible();
    await page.waitForTimeout(300);

    await expect(page).toHaveScreenshot("settings.png", { fullPage: true });
    await toggleTheme(page);
    await expect(page).toHaveScreenshot("settings-alt-theme.png", { fullPage: true });
  });
});
