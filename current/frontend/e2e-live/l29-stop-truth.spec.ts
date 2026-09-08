/**
 * L29 — Stop must be true, end to end on the real stack.
 *
 * S1 measured the defect on 2026-09-02: Stop on a live deep-research run
 * published `status IDLE detail="cancelled"` 29 ms later, every run control
 * disappeared, and no checkpoint ever arrived — while the engine kept emitting
 * `review` / `rework` for another eight minutes.
 *
 * This spec drives the fixed path through the product UI and saves the frames
 * and screenshots as evidence:
 *
 *   1. start a real run and wait until it is in the WRITER phase (the phase the
 *      engine could not observe Stop in at all);
 *   2. press Stop → the stopping wall, and the Stop control replaced by a
 *      disabled "Stopping…";
 *   3. wait for the run's OWN ending: `research_checkpoint` + PAUSED, the
 *      checkpoint panel with Resume;
 *   4. press Resume and confirm the run continues from the pool.
 *
 * The Stop LATENCY (press → PAUSED) is recorded, not asserted: it is a
 * measurement of how long the boundary in flight took, and the honest number is
 * whatever it is.
 */
import { expect, test, type Page, type WebSocket } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

import {
  allEvents,
  conversationIdFromWebSocket,
  requireReliabilityStack,
  type EventJson,
} from "./reliability-helpers";

const OUT = process.env.L29_OUT ?? "/tmp/l29-stop";
const TOPIC =
  process.env.L29_TOPIC ??
  "What is the measured state of sodium-ion battery cycle life in 2026?";
const TIER = process.env.L29_TIER ?? "quick";
// Re-prove Resume alone against a conversation already stopped at a checkpoint,
// without spending another full run.
const RESUME_CID = process.env.L29_RESUME_CID ?? "";
// Prove the OTHER user-initiated ending against a run that is already live.
const KILL_CID = process.env.L29_KILL_CID ?? "";
const WRITER_BUDGET_MS = Number(process.env.L29_WRITER_BUDGET_MS ?? 2_400_000);

fs.mkdirSync(OUT, { recursive: true });

const TIER_MENU_LABEL: Record<string, RegExp> = {
  quick: /quick/i,
  standard_deep: /standard/i,
  exhaustive: /exhaustive/i,
};

test.use({ viewport: { width: 1280, height: 1100 } });

/** Poll until the conversation leaves PAUSED, or the budget runs out.
 *
 *  A resumed deep-research run re-enters the RESEARCH loop and only publishes
 *  RUNNING once its preflight passes, so "the log grew" is not the signal —
 *  the reconstruction facts land first. The status is. */
async function waitForRunningAgain(
  request: Parameters<typeof allEvents>[0],
  cid: string,
  budgetMs: number,
): Promise<EventJson[]> {
  const deadline = Date.now() + budgetMs;
  let events = await allEvents(request, cid);
  const paused = () =>
    events.filter((e: EventJson) => e.kind === "status").at(-1)?.status === "PAUSED";
  while (paused() && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 5_000));
    events = await allEvents(request, cid);
  }
  return events;
}

test("L29: Stop during the writer phase lands a resumable checkpoint", async ({
  page,
  request,
}) => {
  test.setTimeout(WRITER_BUDGET_MS + 1_800_000);
  await requireReliabilityStack(request);

  const notes: Array<Record<string, unknown>> = [];
  const frameLog = path.join(OUT, "stop-ws-frames.jsonl");
  let frameCount = 0;
  // The run's cid comes off the socket the surface opens: the composer creates
  // the conversation before the route changes, so the URL is not the source.
  let socketCid: string | null = null;
  page.on("websocket", (socket) => {
    socketCid = conversationIdFromWebSocket(socket.url()) ?? socketCid;
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

  async function capture(key: string, note: string) {
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(150);
    const probe = await page.evaluate(() => {
      const el = (sel: string) => document.querySelector(sel) as HTMLElement | null;
      return {
        phase: el("[data-dr-phase]")?.getAttribute("data-dr-phase") ?? null,
        signal: el("[data-dr-signal]")?.getAttribute("data-dr-signal") ?? null,
        stoppingText: el('[data-dr-stopping="panel"]')?.innerText ?? null,
        checkpointText: el('[data-dr-checkpoint="true"]')?.innerText ?? null,
        killedText: el("[data-dr-killed]")?.innerText ?? null,
        controls: [...document.querySelectorAll("[data-disco-control]")].map((c) =>
          c.getAttribute("data-disco-control"),
        ),
        disabledControls: [...document.querySelectorAll("[data-disco-control]")]
          .filter((c) => (c as HTMLButtonElement).disabled)
          .map((c) => c.getAttribute("data-disco-control")),
      };
    });
    await page.screenshot({ path: path.join(OUT, `${key}.png`) });
    notes.push({ file: `${key}.png`, at: new Date().toISOString(), note, frameIndex: frameCount, ...probe });
    fs.writeFileSync(path.join(OUT, "stop-captures.json"), JSON.stringify(notes, null, 2), "utf-8");
  }

  // ---- 1. start a real run through the product path ------------------------
  await page.goto("/");
  // PKG-28 composer: the surface is a "Search type" radio, and depth lives
  // behind the Options disclosure.
  await page.getByRole("radio", { name: /deep research/i }).click();
  await page.locator('[data-disco-control="dr.options"]').first().click();
  await page.locator('[data-disco-control="dr.depth-tier"]').first().click();
  await page.getByRole("menuitem", { name: TIER_MENU_LABEL[TIER] }).click();
  await page.getByPlaceholder(/ask a research question/i).fill(TOPIC);
  await page.keyboard.press("Enter");
  await page.locator("[data-dr-brief]").waitFor({ state: "visible", timeout: 300_000 });

  const cid = page.url().split("/deep/")[1]?.split(/[?#]/)[0] || (socketCid ?? "");
  expect(cid, "no conversation id after submit").toBeTruthy();
  fs.writeFileSync(path.join(OUT, "cid.txt"), cid, "utf-8");
  await capture("89-brief", "the run started: the model's brief is on screen");

  // ---- 2. wait for the WRITER phase ---------------------------------------
  // The heartbeat's own words for the writer arc; the run must be inside it
  // when Stop is pressed, or this proves nothing the research loop did not
  // already prove.
  const deadline = Date.now() + WRITER_BUDGET_MS;
  let writerLine = "";
  while (Date.now() < deadline) {
    const strip = page.locator("section").filter({ has: page.locator("[data-dr-signal]") }).first();
    if ((await strip.count()) > 0) {
      const text = (await strip.innerText()).replace(/\n/g, " | ");
      const match = /(Writing the report[^|]*|Review \d+ of \d+|Rework \d+ of \d+|Continuation \d+ of \d+)/.exec(text);
      if (match) {
        writerLine = match[1].trim();
        break;
      }
    }
    if ((await page.locator("[data-dr-phase]").first().getAttribute("data-dr-phase")) === "done") {
      break;
    }
    await page.waitForTimeout(2_000);
  }
  expect(writerLine, "the run never reached the writer phase inside the budget").toBeTruthy();
  await capture("90-before-stop", `writer phase in flight: ${writerLine}`);

  // ---- 3. Stop → the stopping wall ----------------------------------------
  const pressedAt = Date.now();
  await page.locator('[data-disco-control="dr.stop"]').first().click();
  const stoppingPanel = page.locator('[data-dr-stopping="panel"]');
  const checkpointPanel = page.locator('[data-dr-checkpoint="true"]');
  await stoppingPanel.or(checkpointPanel).first().waitFor({ state: "visible", timeout: 30_000 });
  // Stop may checkpoint before the browser samples its transient stopping UI.
  // Inspect that UI when present; a fast completed checkpoint is also correct.
  if (await stoppingPanel.isVisible()) {
    await capture("91-stopping-wall", "Stop is still draining its current work");
  }

  // ---- 4. the run's own ending: checkpoint + PAUSED ------------------------
  await page
    .locator('[data-dr-checkpoint="true"]')
    .waitFor({ state: "visible", timeout: 1_800_000 });
  const pausedAt = Date.now();
  await capture("92-stopped-checkpoint", "the run's own ending: checkpoint + Resume");
  await expect(page.locator('[data-disco-control="dr.resume"]')).toBeVisible();

  const events = await allEvents(request, cid);
  fs.writeFileSync(
    path.join(OUT, "stop-events.json"),
    JSON.stringify({ cid, events }, null, 2),
    "utf-8",
  );
  const statuses = events.filter((e: EventJson) => e.kind === "status");
  fs.writeFileSync(
    path.join(OUT, "stop-latency.json"),
    JSON.stringify(
      {
        cid,
        writer_line_when_stopped: writerLine,
        stop_pressed_at: new Date(pressedAt).toISOString(),
        paused_seen_at: new Date(pausedAt).toISOString(),
        stop_latency_seconds: Math.round((pausedAt - pressedAt) / 100) / 10,
        statuses: statuses.map((s) => ({ seq: s.seq, status: s.status, detail: s.detail })),
      },
      null,
      2,
    ),
    "utf-8",
  );

  // The whole defect, asserted on the durable log: a non-terminal marker, a
  // checkpoint, PAUSED/"stopped" — and never IDLE/"cancelled".
  expect(
    events.filter((e) => e.kind === "action" && e.tool_call?.tool_name === "stop_requested"),
  ).toHaveLength(1);
  expect(events.some((e) => e.kind === "research_checkpoint")).toBe(true);
  expect(
    statuses.some((s) => s.status === "IDLE" && s.detail === "cancelled"),
    "Stop must never publish the loop's terminal IDLE on a deep-research run",
  ).toBe(false);
  const terminal = statuses.at(-1);
  expect(terminal?.status).toBe("PAUSED");
  expect(terminal?.detail).toBe("stopped");
  // No report was published from a stopped run.
  expect(events.some((e) => e.kind === "report")).toBe(false);

  // ---- 5. Resume continues from the pool ----------------------------------
  await page.locator('[data-disco-control="dr.resume"]').first().click();
  // The re-entered run researches before it writes, so its first new event is
  // a model turn — minutes, not seconds. Poll rather than sleep once.
  const afterResume = await waitForRunningAgain(request, cid, 900_000);
  await capture("93-resumed", "after Resume: the run is working again from the checkpoint");
  fs.writeFileSync(
    path.join(OUT, "resume-events.json"),
    JSON.stringify({ cid, events: afterResume }, null, 2),
    "utf-8",
  );
  expect(
    afterResume.length,
    "Resume must actually re-enter the run, not silently do nothing",
  ).toBeGreaterThan(events.length);
  expect(
    afterResume.filter((e) => e.kind === "status").at(-1)?.status,
    "resume must leave the conversation working, not paused",
  ).not.toBe("PAUSED");
});

async function freshResearchRun(page: Page): Promise<string> {
  let socketCid: string | null = null;
  const onSocket = (socket: WebSocket) => {
    // The surface can open its socket before navigation commits the new URL.
    socketCid = conversationIdFromWebSocket(socket.url()) ?? socketCid;
  };
  page.on("websocket", onSocket);
  try {
    await page.goto("/");
    await page.getByRole("radio", { name: /deep research/i }).click();
    await page.locator('[data-disco-control="dr.options"]').first().click();
    await page.locator('[data-disco-control="dr.depth-tier"]').first().click();
    await page.getByRole("menuitem", { name: TIER_MENU_LABEL.quick }).click();
    await page.getByPlaceholder(/ask a research question/i).fill(TOPIC);
    await page.keyboard.press("Enter");
    await page.locator("[data-dr-brief]").waitFor({ state: "visible", timeout: 300_000 });
    const cid = page.url().split("/deep/")[1]?.split(/[?#]/)[0] || socketCid || "";
    expect(cid, "fresh research must create a conversation").toBeTruthy();
    return cid;
  } finally {
    page.off("websocket", onSocket);
  }
}

test("L29: Resume alone, on an existing stop checkpoint", async ({ page, request }) => {
  test.setTimeout(1_200_000);
  await requireReliabilityStack(request);
  const cid = RESUME_CID || (await freshResearchRun(page));
  if (!RESUME_CID) {
    await expect.poll(async () => (await allEvents(request, cid)).some(
      (event) => Number(event.tool_result?.structured?.added ?? 0) > 0,
    ), { timeout: 300_000, message: "fresh run must retain evidence before Stop" }).toBe(true);
    await page.locator('[data-disco-control="dr.stop"]').first().click();
    await page.locator('[data-dr-checkpoint="true"]').waitFor({ state: "visible", timeout: 120_000 });
  }
  const before = await allEvents(request, cid);
  expect(before.filter((e) => e.kind === "status").at(-1)?.status).toBe("PAUSED");

  await page.goto(`/deep/${cid}`);
  await page.locator('[data-disco-control="dr.resume"]').first().click();
  const after = await waitForRunningAgain(request, cid, 900_000);
  await page.screenshot({ path: path.join(OUT, "94-resume-only.png") });
  fs.writeFileSync(
    path.join(OUT, "resume-only-events.json"),
    JSON.stringify({ cid, events: after }, null, 2),
    "utf-8",
  );
  expect(after.length).toBeGreaterThan(before.length);
  expect(after.filter((e) => e.kind === "status").at(-1)?.status).not.toBe("PAUSED");
});

test("L29: Kill on a live run ends it as a user choice", async ({ page, request }) => {
  test.setTimeout(600_000);
  await requireReliabilityStack(request);
  const cid = KILL_CID || (await freshResearchRun(page));
  const before = await allEvents(request, cid);
  expect(before.filter((e: EventJson) => e.kind === "status").at(-1)?.status).toBe("RUNNING");

  await page.goto(`/deep/${cid}`);
  await page.locator('[data-disco-control="dr.kill"]').first().click();
  await page.locator("[data-dr-killed]").waitFor({ state: "visible", timeout: 120_000 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(OUT, "95-killed.png") });

  const after = await allEvents(request, cid);
  fs.writeFileSync(
    path.join(OUT, "kill-events.json"),
    JSON.stringify({ cid, events: after }, null, 2),
    "utf-8",
  );
  const terminal = after.filter((e: EventJson) => e.kind === "status").at(-1);
  expect(terminal?.status).toBe("IDLE");
  expect(terminal?.detail).toBe("killed");
  // A kill is a choice, not a failure — and not one "re-verify the workspace"
  // error per turn counter either.
  expect(after.some((e: EventJson) => e.kind === "error")).toBe(false);
  expect(after.some((e: EventJson) => e.kind === "agent_error")).toBe(false);
  // The trace it produced stays on the conversation.
  expect(
    after.filter((e: EventJson) => e.kind === "action").length,
  ).toBeGreaterThan(before.filter((e: EventJson) => e.kind === "action").length - 1);
  // …and the screen says what happened in the user's own terms.
  const notice = await page.locator("[data-dr-killed]").innerText();
  expect(notice).toContain("You killed this run");
  expect(notice).toContain("no checkpoint");
});

test("L29: Stop then Resume writes no observation the run did not make", async ({
  page,
  request,
}) => {
  test.setTimeout(1_800_000);
  await requireReliabilityStack(request);

  let socketCid: string | null = null;
  page.on("websocket", (socket) => {
    socketCid = conversationIdFromWebSocket(socket.url()) ?? socketCid;
  });

  // A fresh run, stopped EARLY — inside the research loop rather than the
  // writer. The boundary Stop is honoured at is not the point here; what is
  // being proved is what RESUME writes to the log afterwards.
  await page.goto("/");
  await page.getByRole("radio", { name: /deep research/i }).click();
  await page.locator('[data-disco-control="dr.options"]').first().click();
  await page.locator('[data-disco-control="dr.depth-tier"]').first().click();
  await page.getByRole("menuitem", { name: TIER_MENU_LABEL.quick }).click();
  await page.getByPlaceholder(/ask a research question/i).fill(TOPIC);
  await page.keyboard.press("Enter");
  await page.locator("[data-dr-brief]").waitFor({ state: "visible", timeout: 300_000 });
  const cid = page.url().split("/deep/")[1]?.split(/[?#]/)[0] || (socketCid ?? "");
  expect(cid).toBeTruthy();
  fs.writeFileSync(path.join(OUT, "cid.txt"), cid, "utf-8");

  // The durable admission is the precondition; heartbeat wording is not evidence.
  await expect.poll(async () => (await allEvents(request, cid)).some(
    (event) => Number(event.tool_result?.structured?.added ?? 0) > 0,
  ), { timeout: 300_000, message: "fresh run must retain evidence before Stop" }).toBe(true);

  await page.locator('[data-disco-control="dr.stop"]').first().click();
  await page
    .locator('[data-dr-checkpoint="true"]')
    .waitFor({ state: "visible", timeout: 900_000 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(OUT, "96-stopped.png") });
  const atPause = await allEvents(request, cid);
  const pausedSeq = Number(
    atPause.filter((e: EventJson) => e.kind === "status").at(-1)?.seq ?? 0,
  );
  expect(pausedSeq).toBeGreaterThan(0);

  await page.locator('[data-disco-control="dr.resume"]').first().click();
  const after = await waitForRunningAgain(request, cid, 900_000);
  fs.writeFileSync(
    path.join(OUT, "resume-events.json"),
    JSON.stringify({ cid, paused_seq: pausedSeq, events: after }, null, 2),
    "utf-8",
  );

  const running = after.find(
    (e: EventJson) => e.kind === "status" && Number(e.seq) > pausedSeq && e.status === "RUNNING",
  );
  expect(running, "the resume must reach RUNNING").toBeTruthy();
  const between = after.filter(
    (e: EventJson) =>
      Number(e.seq) > pausedSeq && Number(e.seq) < Number(running!.seq) && e.kind === "observation",
  );
  // Rule 6: never emit what the system did not observe. Resume used to pair
  // every deep-research progress marker with "This action was interrupted by a
  // server restart — its outcome is UNKNOWN. Re-verify its effect", one per
  // turn counter and per token heartbeat.
  expect(between, "resume wrote observations the run never made").toEqual([]);

  // …and the checkpoint panel does not outlive the run it describes.
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(OUT, "97-resumed-no-checkpoint.png") });
  await expect(page.locator('[data-dr-checkpoint="true"]')).toHaveCount(0);
  await expect(page.locator('[data-disco-control="dr.resume"]')).toHaveCount(0);
  const finalEvents = await allEvents(request, cid);
  if (finalEvents.filter((event) => event.kind === "status").at(-1)?.status === "FINISHED") {
    expect(finalEvents.filter((event) => event.kind === "report")).toHaveLength(1);
  } else {
    await expect(page.locator('[data-disco-control="dr.stop"]')).toBeVisible();
    await page.locator('[data-disco-control="dr.kill"]').first().click();
    await expect.poll(async () => (await allEvents(request, cid))
      .filter((event) => event.kind === "status").at(-1)?.status,
    { timeout: 30_000 }).toMatch(/^(FINISHED|IDLE)$/);
    const settled = await allEvents(request, cid);
    const terminal = settled.filter((event) => event.kind === "status").at(-1);
    if (settled.some((event) => event.kind === "report")) {
      expect(terminal?.status, "late Kill must preserve the report's committed ending").toBe("FINISHED");
      expect(settled.filter((event) => event.kind === "report")).toHaveLength(1);
    } else {
      expect(terminal?.status).toBe("IDLE");
      expect(terminal?.detail).toBe("killed");
      await expect(page.locator("[data-dr-killed]")).toBeVisible();
    }
  }
});
