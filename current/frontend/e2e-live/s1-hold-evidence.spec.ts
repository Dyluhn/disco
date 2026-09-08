/**
 * S1 (b) — live evidence for the deep-research HOLD panel (L24).
 *
 * Drives a real `standard_deep` run against an ISOLATED agent-server whose
 * search pool is genuinely cooling (the upstream engines behind the self-hosted
 * SearXNG are returning "Suspended: too many requests"). Nothing is faked: the
 * panel appears only when the engine emits a real `hold` action event.
 *
 * Output dir: S1_OUT (absolute).
 */
import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

import { allEvents, requireReliabilityStack, conversationIdFromWebSocket } from "./reliability-helpers";

const OUT = process.env.S1_OUT ?? "/tmp/s1-hold";
const QUESTION =
  process.env.S1_QUESTION ??
  "What does the primary literature say about long-duration iron-air battery storage economics, and where do the cost estimates disagree?";
const TIER = process.env.S1_TIER ?? "standard_deep";

fs.mkdirSync(OUT, { recursive: true });

test("S1 hold: panel, Continue waiting, Stop checkpoint", async ({ page, request }) => {
  test.setTimeout(3_600_000);
  await requireReliabilityStack(request);

  const frameLog = path.join(OUT, "hold-ws-frames.jsonl");
  let frameCount = 0;
  let cid = "";
  page.on("websocket", (socket) => {
    cid ||= conversationIdFromWebSocket(socket.url()) ?? "";
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
  function record(entry: Record<string, unknown>) {
    notes.push({ at: new Date().toISOString(), ...entry });
    fs.writeFileSync(path.join(OUT, "hold-captures.json"), JSON.stringify(notes, null, 2), "utf-8");
  }

  async function shot(key: string, note: string, sel?: string) {
    await page.screenshot({ path: path.join(OUT, `${key}.png`) });
    let text: string[] = [];
    if (sel) {
      const el = page.locator(sel).first();
      if ((await el.count()) > 0) {
        try {
          await el.screenshot({ path: path.join(OUT, `${key}-el.png`) });
          text = (await el.innerText()).split("\n").map((s) => s.trim()).filter(Boolean);
        } catch {
          /* moved */
        }
      }
    }
    record({ file: `${key}.png`, note, text, frameIndex: frameCount });
  }

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
  await page.getByRole("menuitem", { name: TIER_LABEL[TIER] ?? /^Standard/ }).click();
  await page.keyboard.press("Escape");
  await input.fill(QUESTION);
  await input.press("Enter");

  const panel = page.locator('[data-dr-hold="panel"]');

  // 1 — wait for a REAL hold.
  const deadline = Date.now() + 2_400_000;
  let sawPanel = false;
  let lastLog = 0;
  while (Date.now() < deadline) {
    if ((await panel.count()) > 0) {
      sawPanel = true;
      break;
    }
    const phase = await page.locator("[data-dr-phase]").first().getAttribute("data-dr-phase");
    if (phase === "done" || phase === "error") break;
    if (Date.now() - lastLog > 20_000) {
      lastLog = Date.now();
      const sig = page.locator("[data-dr-signal]").first();
      const lvl = (await sig.count()) > 0 ? await sig.getAttribute("data-dr-signal") : "-";
      console.log(`[s1-hold] ${new Date().toISOString()} phase=${phase} signal=${lvl}`);
    }
    await page.waitForTimeout(700);
  }
  expect(sawPanel, "no real hold occurred within the window").toBe(true);
  await shot("40-hold-panel", "hold decision panel — from a real `hold` action event", '[data-dr-hold="panel"]');

  // 2 — Continue waiting: a LOCAL acknowledgement, sends nothing.
  const framesBefore = frameCount;
  await page.locator('[data-disco-control="dr.hold.continue"]').first().click();
  await page.waitForTimeout(2_500);
  record({ note: "frames sent between the panel shot and 2.5s after Continue", framesBefore, framesAfter: frameCount });
  await shot("41-hold-continued", "after Continue waiting — collapsed hold line, Stop still reachable", '[data-dr-hold="line"]');

  // 3 — a LATER hold (new episode) re-opens the panel; otherwise Stop from the
  //     collapsed line, which is the same control.
  const stopDeadline = Date.now() + 900_000;
  let secondPanel = false;
  while (Date.now() < stopDeadline) {
    if ((await panel.count()) > 0) {
      secondPanel = true;
      break;
    }
    const phase = await page.locator("[data-dr-phase]").first().getAttribute("data-dr-phase");
    if (phase === "done" || phase === "error") break;
    await page.waitForTimeout(1_000);
  }
  record({ note: "second hold episode re-opened the panel", secondPanel });
  if (secondPanel) {
    await shot("42-hold-panel-second", "a SECOND hold episode re-asks the question", '[data-dr-hold="panel"]');
  }

  const stopButton = page.locator('[data-disco-control="dr.hold.stop"]').first();
  if ((await stopButton.count()) > 0) {
    await stopButton.click();
  } else {
    // The hold ended; Stop from the top bar is the same cancel frame.
    await page.getByRole("button", { name: /^stop$/i }).first().click();
  }
  await page.waitForTimeout(4_000);
  await shot("43-after-stop", "immediately after Stop");

  const checkpoint = page.locator('[data-dr-checkpoint="true"]');
  await checkpoint.waitFor({ state: "visible", timeout: 300_000 }).catch(() => {});
  await page.waitForTimeout(1_000);
  await shot("44-stopped-checkpoint", "the stopped state as the user sees it", '[data-dr-checkpoint="true"]');
  const errState = page.getByText(/research run failed|error/i).first();
  record({
    note: "post-stop DOM probes",
    checkpointVisible: (await checkpoint.count()) > 0,
    phase: await page.locator("[data-dr-phase]").first().getAttribute("data-dr-phase"),
    errorish: (await errState.count()) > 0 ? await errState.innerText().catch(() => "") : null,
  });

  if (cid) {
    const events = await allEvents(request, cid);
    fs.writeFileSync(path.join(OUT, "hold-events.json"), JSON.stringify({ cid, events }, null, 2), "utf-8");
    fs.writeFileSync(path.join(OUT, "hold-cid.txt"), cid, "utf-8");
  }
});
