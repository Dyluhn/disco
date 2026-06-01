import { chromium } from "playwright";

const URL = "http://localhost:5180";
const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1320, height: 900 },
  deviceScaleFactor: 2,
});
const page = await ctx.newPage();

await page.goto(URL, { waitUntil: "networkidle" });
await page.waitForTimeout(500); // let webfonts settle
await page.screenshot({ path: "shots/1-empty-dark.png" });

// run a query → wait for the FINISHED signal (the follow-up pill)
await page
  .getByPlaceholder(/ask anything/i)
  .fill("How does reciprocal rank fusion work, and when should I use it?");
await page.keyboard.press("Enter");
await page.getByRole("button", { name: /How is k chosen/i }).waitFor({ timeout: 25000 });
await page.waitForTimeout(300);
await page.screenshot({ path: "shots/2-answer-dark.png", fullPage: true });

// citation hover card (desktop) near the top of the document
await page.evaluate(() => window.scrollTo(0, 0));
const cite = page.getByRole("button", { name: /^Source 1/i }).first();
await cite.hover();
await page.waitForTimeout(450);
await page.screenshot({ path: "shots/3-citation-dark.png" });

// toggle to light and re-capture the answer
await page.getByRole("button", { name: /switch to light/i }).click();
await page.waitForTimeout(350);
await page.screenshot({ path: "shots/4-answer-light.png", fullPage: true });

await browser.close();
console.log("done");
