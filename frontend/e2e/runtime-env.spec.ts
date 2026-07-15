import { expect, test } from "@playwright/test";

test("Vite serves the runtime bootstrap without a failed-script console warning", async ({ page }) => {
  const consoleWarnings: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "warning" || message.type() === "error") {
      consoleWarnings.push(message.text());
    }
  });

  const envResponsePromise = page.waitForResponse((response) => response.url().endsWith("/env.js"));
  await page.goto("/");
  const envResponse = await envResponsePromise;

  expect(envResponse.status()).toBe(200);
  expect(envResponse.headers()["content-type"]).toContain("javascript");
  await expect
    .poll(() => page.evaluate(() => (globalThis as { __DISCO_ENV?: unknown }).__DISCO_ENV))
    .toEqual({});
  expect(consoleWarnings).toEqual([]);
});
