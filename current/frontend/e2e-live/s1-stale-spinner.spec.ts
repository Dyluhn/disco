/**
 * S1 (d) — one frame that shows the honest heartbeat and the status-driven
 * spinner at the same instant.
 *
 * `deepResearchTraceParts/liveTrace.ts` sets a trace row's status to "running"
 * from `ConversationStatus === "RUNNING"`, not from any event, and ActivityFeed
 * renders an `animate-spin` loader + a "now" tag for it. So while the strip
 * header says "no signal for 1:12", the trace underneath is still animating.
 * This capture puts both in one screenshot (tall viewport) and records the
 * spinner count next to the signal level at that instant.
 *
 * Read-only: opens an EXISTING conversation on /deep/:cid (subscribe + replay).
 */
import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

const OUT = process.env.S1_OUT ?? "/tmp/s1-spin";
const CID = process.env.S1_CID ?? "";
const MINUTES = Number(process.env.S1_MINUTES ?? "10");

fs.mkdirSync(OUT, { recursive: true });

test.use({ viewport: { width: 1280, height: 2200 } });

test("S1: status-driven spinner still animates while the heartbeat says no signal", async ({ page }) => {
  test.setTimeout(1_800_000);
  expect(CID, "S1_CID is required").toBeTruthy();

  const notes: Array<Record<string, unknown>> = [];
  const done = new Set<string>();

  async function capture(key: string, note: string) {
    if (done.has(key)) return;
    const probe = await page.evaluate(() => {
      const sig = document.querySelector("[data-dr-signal]");
      const spinners = document.querySelectorAll(".animate-spin");
      const nowTags = [...document.querySelectorAll("li")].filter((li) =>
        (li.textContent ?? "").trim().endsWith("now"),
      ).length;
      return {
        level: sig?.getAttribute("data-dr-signal") ?? null,
        label: (sig as HTMLElement | null)?.innerText?.trim() ?? null,
        spinners: spinners.length,
        nowTags,
      };
    });
    await page.screenshot({ path: path.join(OUT, `${key}.png`) });
    notes.push({ file: `${key}.png`, at: new Date().toISOString(), note, ...probe });
    done.add(key);
    fs.writeFileSync(path.join(OUT, "spinner-captures.json"), JSON.stringify(notes, null, 2), "utf-8");
  }

  await page.goto(`/deep/${CID}`);
  const deadline = Date.now() + MINUTES * 60_000;
  while (Date.now() < deadline) {
    const sig = page.locator("[data-dr-signal]").first();
    if ((await sig.count()) > 0) {
      const level = await sig.getAttribute("data-dr-signal");
      if (level === "silent") await capture("60-silent-with-spinner", "strip header says no signal; trace rows still animate");
      if (level === "quiet") await capture("61-quiet-with-spinner", "strip header says quiet; trace rows still animate");
      if (done.has("60-silent-with-spinner") && done.has("61-quiet-with-spinner")) break;
    }
    await page.waitForTimeout(800);
  }
  fs.writeFileSync(path.join(OUT, "spinner-captures.json"), JSON.stringify(notes, null, 2), "utf-8");
});
