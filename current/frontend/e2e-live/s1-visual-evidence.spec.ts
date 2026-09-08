/**
 * S1 — live visual evidence for the wave-1 deep-research run UI (L24).
 *
 * Evidence only: drives a REAL `quick` deep-research run against the live
 * agent-server, records every WebSocket frame verbatim, and screenshots the
 * heartbeat / activity-signal / bounded-notice states as they actually occur.
 * Nothing here fabricates an event or a state.
 *
 * Output dir: S1_OUT (absolute).
 */
import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

import { allEvents, deleteConversation, requireReliabilityStack, conversationIdFromWebSocket } from "./reliability-helpers";

const OUT = process.env.S1_OUT ?? "/tmp/s1-out";
const QUESTION =
  process.env.S1_QUESTION ??
  "What does peer-reviewed evidence say about the durability of grid-scale sodium-ion battery cells compared with LFP, and what are the main open questions?";
const TIER = process.env.S1_TIER ?? "quick";
const KEEP = process.env.S1_KEEP === "1";

fs.mkdirSync(OUT, { recursive: true });

type Capture = {
  file: string;
  atIso: string;
  signalLevel: string | null;
  signalLabel: string | null;
  statsRow: string | null;
  heartbeat: string[];
  frameIndex: number;
  note: string;
};

test("S1: live heartbeat states, hold (if any) and bounded notice", async ({ page, request }, testInfo) => {
  test.setTimeout(3_600_000);
  await requireReliabilityStack(request);

  const frameLog = path.join(OUT, "ws-frames.jsonl");
  const frames: string[] = [];
  let cid = "";
  page.on("websocket", (socket) => {
    cid ||= conversationIdFromWebSocket(socket.url()) ?? "";
    socket.on("framereceived", (data) => {
      const payload = typeof data.payload === "string" ? data.payload : data.payload.toString("utf-8");
      const line = JSON.stringify({ i: frames.length, at: new Date().toISOString(), dir: "recv", payload });
      frames.push(line);
      fs.appendFileSync(frameLog, line + "\n");
    });
    socket.on("framesent", (data) => {
      const payload = typeof data.payload === "string" ? data.payload : data.payload.toString("utf-8");
      const line = JSON.stringify({ i: frames.length, at: new Date().toISOString(), dir: "sent", payload });
      frames.push(line);
      fs.appendFileSync(frameLog, line + "\n");
    });
  });

  const captures: Capture[] = [];
  const done = new Set<string>();

  async function snapshot() {
    const signal = page.locator("[data-dr-signal]").first();
    const level = (await signal.count()) > 0 ? await signal.getAttribute("data-dr-signal") : null;
    const label = (await signal.count()) > 0 ? (await signal.innerText()).trim() : null;
    const strip = page.locator("section:has([data-dr-brief]), section").first();
    let statsRow: string | null = null;
    let heartbeat: string[] = [];
    const hb = page.locator("[data-dr-brief]").first();
    try {
      const stripEl = page.locator("section").filter({ has: page.locator("[data-dr-signal]") }).first();
      if ((await stripEl.count()) > 0) {
        const txt = (await stripEl.innerText()).trim();
        heartbeat = txt.split("\n").map((s) => s.trim()).filter(Boolean).slice(0, 12);
        statsRow = heartbeat[0] ?? null;
      }
    } catch {
      /* transient re-render */
    }
    void strip;
    void hb;
    return { level, label, statsRow, heartbeat };
  }

  async function capture(key: string, note: string) {
    if (done.has(key)) return false;
    const s = await snapshot();
    const file = `${key}.png`;
    await page.screenshot({ path: path.join(OUT, file) });
    // Also a tight shot of the run region when it exists.
    const strip = page.locator("section").filter({ has: page.locator("[data-dr-signal]") }).first();
    if ((await strip.count()) > 0) {
      try {
        await strip.screenshot({ path: path.join(OUT, `${key}-strip.png`) });
      } catch {
        /* element moved */
      }
    }
    captures.push({
      file,
      atIso: new Date().toISOString(),
      signalLevel: s.level,
      signalLabel: s.label,
      statsRow: s.statsRow,
      heartbeat: s.heartbeat,
      frameIndex: frames.length,
      note,
    });
    done.add(key);
    fs.writeFileSync(path.join(OUT, "captures.json"), JSON.stringify(captures, null, 2), "utf-8");
    return true;
  }

  try {
    const TIER_LABEL: Record<string, RegExp> = {
      quick: /^Quick/,
      standard_deep: /^Standard/,
      exhaustive: /^Thorough/,
    };
    await page.goto("/");
    await page.getByRole("radio", { name: /deep research/i }).click();
    const input = page.getByPlaceholder(/ask a research question/i);
    await input.waitFor({ state: "visible", timeout: 30_000 });
    await page.locator('[data-disco-control="dr.options"]').click();
    await page.locator('[data-disco-control="dr.depth-tier"]').click();
    await page.getByRole("menuitem", { name: TIER_LABEL[TIER] ?? /^Quick/ }).click();
    await page.keyboard.press("Escape");
    await input.fill(QUESTION);
    await input.press("Enter");

    // Start-up state: no run signal yet.
    for (let i = 0; i < 40; i += 1) {
      if ((await page.locator("[data-dr-starting]").count()) > 0) {
        await capture("00-starting", "StartingState — no engine event has arrived yet");
        break;
      }
      await page.waitForTimeout(250);
    }

    const deadline = Date.now() + 3_000_000;
    let finished = false;
    let lastLog = 0;
    while (Date.now() < deadline) {
      const phase = await page.locator("[data-dr-phase]").first().getAttribute("data-dr-phase");
      if (phase === "done" || phase === "error" || phase === "paused") {
        finished = true;
        break;
      }
      const s = await snapshot();
      const text = s.heartbeat.join(" | ");

      if ((await page.locator('[data-dr-hold="panel"]').count()) > 0) {
        await capture("20-hold-panel", "hold panel — real `hold` action event on this run");
      }
      if (s.level === "live") {
        if (/Turn \d+ of \d+/.test(text)) await capture("01-live-turn", "signal=live, heartbeat now-line is the turn");
        if (/Searching:/.test(text)) await capture("02-live-search", "signal=live, now-line is the search query");
        if (/Read \d+ of \d+ pages|Read the results/.test(text)) {
          await capture("03-live-observation", "signal=live, now-line is the observation rollup");
        }
        if (/Review \d+ of \d+/.test(text)) await capture("04-live-review", "signal=live, now-line is a review round");
        if (/Rework \d+ of \d+/.test(text)) await capture("05-live-rework", "signal=live, now-line is a rework round");
        if (/Writing the report/.test(text)) await capture("06-live-writing", "signal=live, writer phase");
      }
      if (s.level === "quiet") {
        await capture("10-quiet", "signal=quiet (20-60 s since the newest event)");
        // A later, deeper quiet is worth a second frame.
        const secs = Number(/quiet for (\d+) s/.exec(s.label ?? "")?.[1] ?? "0");
        if (secs >= 45) await capture("11-quiet-deep", "signal=quiet, past 45 s");
      }
      if (s.level === "silent") {
        await capture("12-silent", "signal=silent (>=60 s), QuietNote attached");
      }

      if (Date.now() - lastLog > 15_000) {
        lastLog = Date.now();
        console.log(`[s1] ${new Date().toISOString()} signal=${s.level} :: ${text.slice(0, 180)}`);
      }
      await page.waitForTimeout(700);
    }

    expect(finished, "run did not reach a terminal phase in time").toBe(true);
    await page.waitForTimeout(1_500);
    await capture("30-finished", "terminal phase reached");

    const notice = page.locator('aside[aria-label="What this run did not close"]');
    if ((await notice.count()) > 0) {
      await notice.scrollIntoViewIfNeeded();
      await page.waitForTimeout(400);
      await page.screenshot({ path: path.join(OUT, "31-bounded-notice.png") });
      await notice.screenshot({ path: path.join(OUT, "31-bounded-notice-el.png") });
      captures.push({
        file: "31-bounded-notice.png",
        atIso: new Date().toISOString(),
        signalLevel: null,
        signalLabel: null,
        statsRow: null,
        heartbeat: (await notice.innerText()).split("\n").map((s) => s.trim()).filter(Boolean),
        frameIndex: frames.length,
        note: "DeepBoundedNotice from the finished ReportEvent",
      });
    }

    if (cid) {
      const events = await allEvents(request, cid);
      fs.writeFileSync(path.join(OUT, "events.json"), JSON.stringify({ cid, events }, null, 2), "utf-8");
      const report = events.filter((e) => e.kind === "report").at(-1);
      fs.writeFileSync(path.join(OUT, "report-event.json"), JSON.stringify(report ?? null, null, 2), "utf-8");
    }
    fs.writeFileSync(path.join(OUT, "captures.json"), JSON.stringify(captures, null, 2), "utf-8");
    fs.writeFileSync(path.join(OUT, "cid.txt"), cid, "utf-8");
  } finally {
    fs.writeFileSync(path.join(OUT, "captures.json"), JSON.stringify(captures, null, 2), "utf-8");
    if (cid && !KEEP) {
      // evidence is already saved; the conversation itself is left in place
      // deliberately when S1_KEEP=1.
      void deleteConversation;
    }
    void testInfo;
  }
});
