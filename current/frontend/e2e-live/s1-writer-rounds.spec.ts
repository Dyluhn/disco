/**
 * S1 (e) — clean header+heartbeat frames for the writer rounds (review /
 * rework / writing) and the honest signal beside them. Scrolls to the top
 * before every shot so the stats row and the heartbeat are always in frame.
 *
 * Read-only: opens an EXISTING conversation on /deep/:cid (subscribe + replay).
 */
import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

const OUT = process.env.S1_OUT ?? "/tmp/s1-writer";
const CID = process.env.S1_CID ?? "";
const MINUTES = Number(process.env.S1_MINUTES ?? "25");

fs.mkdirSync(OUT, { recursive: true });

test.use({ viewport: { width: 1280, height: 900 } });

test("S1 writer rounds: review / rework / writing heartbeat lines", async ({ page }) => {
  test.setTimeout(2_400_000);
  expect(CID, "S1_CID is required").toBeTruthy();

  const notes: Array<Record<string, unknown>> = [];
  const done = new Set<string>();

  async function capture(key: string, note: string) {
    if (done.has(key)) return;
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(120);
    const probe = await page.evaluate(() => {
      const sig = document.querySelector("[data-dr-signal]");
      const strip = sig?.closest("section");
      return {
        level: sig?.getAttribute("data-dr-signal") ?? null,
        label: (sig as HTMLElement | null)?.innerText?.trim() ?? null,
        strip: (strip as HTMLElement | null)?.innerText?.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 10) ?? [],
      };
    });
    await page.screenshot({ path: path.join(OUT, `${key}.png`) });
    notes.push({ file: `${key}.png`, at: new Date().toISOString(), note, ...probe });
    done.add(key);
    fs.writeFileSync(path.join(OUT, "writer-captures.json"), JSON.stringify(notes, null, 2), "utf-8");
  }

  await page.goto(`/deep/${CID}`);
  const deadline = Date.now() + MINUTES * 60_000;
  while (Date.now() < deadline) {
    const strip = page.locator("section").filter({ has: page.locator("[data-dr-signal]") }).first();
    if ((await strip.count()) > 0) {
      const text = (await strip.innerText()).replace(/\n/g, " | ");
      if (/Review \d+ of \d+/.test(text)) await capture("70-review-round", "heartbeat now-line is a review round");
      if (/Rework \d+ of \d+/.test(text)) await capture("71-rework-round", "heartbeat now-line is a rework round");
      if (/Writing the report/.test(text)) await capture("72-writing", "heartbeat now-line is the writer phase");
      if (/Read \d+ of \d+ pages|Read the results/.test(text)) {
        await capture("73-observation", "heartbeat now-line is the observation rollup");
      }
    }
    const phase = await page.locator("[data-dr-phase]").first().getAttribute("data-dr-phase");
    if (phase === "done" || phase === "error") break;
    await page.waitForTimeout(600);
  }
  fs.writeFileSync(path.join(OUT, "writer-captures.json"), JSON.stringify(notes, null, 2), "utf-8");
});
