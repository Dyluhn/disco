import { chromium } from "playwright";

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1320, height: 900 },
  deviceScaleFactor: 2,
  locale: "en-US",
  userAgent:
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
});

const targets = [
  ["perplexity", "https://www.perplexity.ai"],
  ["manus", "https://manus.im"],
  ["manus-ai", "https://manus.ai"],
];

for (const [name, url] of targets) {
  const page = await ctx.newPage();
  try {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
    await page.waitForTimeout(4000); // let hero render/animate
    await page.screenshot({ path: `refs/${name}.png` });
    console.log(name, "ok :: title=", JSON.stringify(await page.title()), "url=", page.url());
  } catch (e) {
    console.log(name, "FAILED ::", e.message);
  } finally {
    await page.close();
  }
}

await browser.close();
console.log("done");
