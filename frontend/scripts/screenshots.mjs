// Capture the Build surface in both themes against the offline fixture trace.
// Usage: dev server on :5173, then `node scripts/screenshots.mjs`.
// NOTE: uses Playwright's Firefox — the bundled chromium fails to rasterize text on
// this host (renders blank glyphs); Firefox uses the system fonts cleanly.
import { firefox } from "playwright";
import { mkdir } from "node:fs/promises";

const BASE = process.env.SHOT_BASE ?? "http://localhost:5173";
const OUT = "screenshots";

async function setTheme(page, theme) {
  await page.evaluate((t) => {
    document.documentElement.classList.remove("dark", "light");
    document.documentElement.classList.add(t);
  }, theme);
  await page.waitForTimeout(120);
}

async function shot(page, name) {
  for (const theme of ["dark", "light"]) {
    await setTheme(page, theme);
    await page.evaluate(() => document.fonts.ready); // avoid the FOIT blank-text race
    await page.waitForTimeout(150);
    await page.screenshot({ path: `${OUT}/${name}-${theme}.png`, fullPage: true });
    console.log(`  ✓ ${name}-${theme}.png`);
  }
  await setTheme(page, "dark");
}

const browser = await firefox.launch();
try {
  await mkdir(OUT, { recursive: true });
  const page = await browser.newPage({ viewport: { width: 1320, height: 940 } });
  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready); // let the web fonts load before capture

  // 1. Build, empty — the task input
  await page.getByRole("radio", { name: "build" }).click();
  await page.getByPlaceholder(/describe a task/i).waitFor();
  await shot(page, "build-empty");

  // 2. submit a task → the trace streams to the confirmation gate
  await page.getByPlaceholder(/describe a task/i).fill("Write fizzbuzz, run it, then clean up.");
  await page.keyboard.press("Enter");
  await page.getByRole("alertdialog", { name: /approval/i }).waitFor({ timeout: 10000 });
  await page.waitForTimeout(200);
  await shot(page, "build-trace-confirm"); // trace + the confirmation gate, both themes

  // 3. approve → the run finishes
  await page.getByRole("button", { name: /approve & run/i }).click();
  await page.getByText(/fizzbuzz ran correctly/i).waitFor({ timeout: 10000 });
  await page.waitForTimeout(200);
  await shot(page, "build-finished");

  console.log("done");
} finally {
  await browser.close();
}
