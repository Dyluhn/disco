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
  test("standard search splash — both themes", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByPlaceholder(/ask anything/i)).toBeVisible();

    await expect(page).toHaveScreenshot("standard-search-splash.png");
    await toggleTheme(page);
    await expect(page).toHaveScreenshot("standard-search-splash-alt-theme.png");
  });

  test("deep research splash — both themes", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "Deep Research" }).click();
    await expect(page.getByPlaceholder(/ask a research question/i)).toBeVisible();

    await expect(page).toHaveScreenshot("deep-research-splash.png");
    await toggleTheme(page);
    await expect(page).toHaveScreenshot("deep-research-splash-alt-theme.png");
  });

  test("research answer — both themes", async ({ page }) => {
    await page.goto("/");
    await page
      .getByPlaceholder(/ask anything/i)
      .fill("How does reciprocal rank fusion work, and when should I use it?");
    await page.keyboard.press("Enter");
    await expect(page.getByRole("tab", { name: /cited/i })).toBeVisible();
    // The source tabs mount at count zero before the fixture has finished. A
    // screenshot at that point is a race-shaped baseline, not visual evidence.
    await expect(page.getByRole("button", { name: /stop generating/i })).toBeHidden({
      timeout: 15_000,
    });
    await expect(page.getByRole("tab", { name: /cited\s*4/i })).toBeVisible();
    await expect(page.getByRole("heading", { name: /when to use it/i })).toBeVisible();

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

    // Settings owns an internal scroll region. A browser full-page capture only
    // adds blank document height; the viewport is the actual rendered surface.
    await expect(page).toHaveScreenshot("settings.png");
    await toggleTheme(page);
    await expect(page).toHaveScreenshot("settings-alt-theme.png");
  });

  test("settings model and media groups — both themes", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: /^settings$/i })).toBeVisible();
    await page.waitForTimeout(300);

    const models = page.locator("#models");
    const media = page.locator("#research-media");
    const generatedMedia = page.locator("#image-generation");
    await models.evaluate((element) => element.scrollIntoView({ block: "start" }));
    await page.waitForTimeout(200);
    await expect(page).toHaveScreenshot("settings-models.png");
    await media.evaluate((element) => element.scrollIntoView({ block: "start" }));
    await page.waitForTimeout(200);
    await expect(page).toHaveScreenshot("settings-research-media.png");
    await generatedMedia.evaluate((element) => element.scrollIntoView({ block: "start" }));
    await page.waitForTimeout(200);
    await expect(page).toHaveScreenshot("settings-generated-media.png");
    await toggleTheme(page);
    await models.evaluate((element) => element.scrollIntoView({ block: "start" }));
    await page.waitForTimeout(200);
    await expect(page).toHaveScreenshot("settings-models-alt-theme.png");
    await media.evaluate((element) => element.scrollIntoView({ block: "start" }));
    await page.waitForTimeout(200);
    await expect(page).toHaveScreenshot("settings-research-media-alt-theme.png");
    await generatedMedia.evaluate((element) => element.scrollIntoView({ block: "start" }));
    await page.waitForTimeout(200);
    await expect(page).toHaveScreenshot("settings-generated-media-alt-theme.png");
  });
});
