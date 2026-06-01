import { chromium } from "playwright";

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1320, height: 900 },
  deviceScaleFactor: 2,
  locale: "en-US",
  colorScheme: "dark", // emulate prefers-color-scheme: dark
  userAgent:
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
});

for (const [name, url] of [
  ["perplexity-dark", "https://www.perplexity.ai"],
  ["manus-dark", "https://manus.im"],
]) {
  const page = await ctx.newPage();
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForTimeout(4500);
    // report the page's actual background so we can tell if it honored dark
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    await page.screenshot({ path: `refs/${name}.png` });
    console.log(name, "ok :: bodyBg=", bg, "url=", page.url());
  } catch (e) {
    console.log(name, "FAILED ::", e.message);
  } finally {
    await page.close();
  }
}

await browser.close();
console.log("done");
