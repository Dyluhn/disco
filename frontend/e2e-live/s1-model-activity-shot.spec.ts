/**
 * S1 (c) — heartbeat evidence for a run whose backend DOES emit
 * `model_activity` (the current tree), for contrast with the wave-1 server that
 * does not. Opens an EXISTING conversation read-only (/deep/:cid — subscribe +
 * replay, never a kick) and screenshots the live heartbeat states.
 *
 * S1_CID = the conversation to watch. S1_OUT = output dir.
 */
import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

const OUT = process.env.S1_OUT ?? "/tmp/s1-ma";
const CID = process.env.S1_CID ?? "";
const MINUTES = Number(process.env.S1_MINUTES ?? "8");

fs.mkdirSync(OUT, { recursive: true });

test("S1 model_activity: heartbeat durations sourced from the open model stream", async ({ page }) => {
  test.setTimeout(1_800_000);
  expect(CID, "S1_CID is required").toBeTruthy();

  const frameLog = path.join(OUT, "ma-ws-frames.jsonl");
  let frameCount = 0;
  page.on("websocket", (socket) => {
    const appendFrame = (dir: "framereceived" | "framesent", payload: string) =>
      fs.appendFileSync(
        frameLog,
        JSON.stringify({ i: frameCount++, at: new Date().toISOString(), dir, payload }) + "\n",
      );
    socket.on("framereceived", ({ payload }) =>
      appendFrame("framereceived", typeof payload === "string" ? payload : payload.toString("utf-8")),
    );
    socket.on("framesent", ({ payload }) =>
      appendFrame("framesent", typeof payload === "string" ? payload : payload.toString("utf-8")),
    );
  });

  const notes: Array<Record<string, unknown>> = [];
  const done = new Set<string>();
  async function capture(key: string, note: string) {
    if (done.has(key)) return;
    const strip = page.locator("section").filter({ has: page.locator("[data-dr-signal]") }).first();
    let text: string[] = [];
    if ((await strip.count()) > 0) {
      text = (await strip.innerText()).split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 12);
    }
    await page.screenshot({ path: path.join(OUT, `${key}.png`) });
    if ((await strip.count()) > 0) {
      try {
        await strip.screenshot({ path: path.join(OUT, `${key}-strip.png`) });
      } catch {
        /* moved */
      }
    }
    const sig = page.locator("[data-dr-signal]").first();
    notes.push({
      file: `${key}.png`,
      at: new Date().toISOString(),
      note,
      signal: (await sig.count()) > 0 ? await sig.getAttribute("data-dr-signal") : null,
      label: (await sig.count()) > 0 ? (await sig.innerText()).trim() : null,
      text,
      frameIndex: frameCount,
    });
    done.add(key);
    fs.writeFileSync(path.join(OUT, "ma-captures.json"), JSON.stringify(notes, null, 2), "utf-8");
  }

  await page.goto(`/deep/${CID}`);
  const deadline = Date.now() + MINUTES * 60_000;
  while (Date.now() < deadline) {
    const strip = page.locator("section").filter({ has: page.locator("[data-dr-signal]") }).first();
    if ((await strip.count()) > 0) {
      const text = (await strip.innerText()).replace(/\n/g, " | ");
      const sig = page.locator("[data-dr-signal]").first();
      const level = await sig.getAttribute("data-dr-signal");
      if (/Turn \d+ of \d+ · thinking \d/.test(text)) {
        await capture("50-model-activity-thinking", "turn duration sourced from `model_activity.seconds`");
      }
      if (level === "quiet") await capture("51-quiet-current-tree", "quiet state on the current tree");
      if (level === "silent") await capture("52-silent-current-tree", "silent state on the current tree");
      if (/Review \d+ of \d+/.test(text)) await capture("53-review-round", "review round line");
      if (/Rework \d+ of \d+/.test(text)) await capture("54-rework-round", "rework round line");
      if (/Writing the report/.test(text)) await capture("55-writing", "writer phase with word count");
    }
    await page.waitForTimeout(800);
  }
  fs.writeFileSync(path.join(OUT, "ma-captures.json"), JSON.stringify(notes, null, 2), "utf-8");
});
