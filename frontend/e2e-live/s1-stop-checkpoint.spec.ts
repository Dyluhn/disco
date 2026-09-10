/**
 * S1 (f) — what Stop actually produces, as the user sees it.
 *
 * The hold panel's Stop is `r.stop` — the same `cancel` frame the top bar's
 * Stop sends (DeepResearchRunView passes `onStop={r.stop}`), so this captures
 * the identical outcome path without needing a hold to exist. The question this
 * answers: does stopping read as a USER CHOICE with the sources retained, or as
 * a failure?
 *
 * Opens an EXISTING run on /deep/:cid, screenshots it live, presses Stop, and
 * screenshots the stopped state.
 */
import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

import { allEvents, requireReliabilityStack } from "./reliability-helpers";

const OUT = process.env.S1_OUT ?? "/tmp/s1-stop";
const CID = process.env.S1_CID ?? "";

fs.mkdirSync(OUT, { recursive: true });

test.use({ viewport: { width: 1280, height: 1000 } });

test("S1 stop: the stopped checkpoint after a user Stop", async ({ page, request }) => {
  test.setTimeout(900_000);
  expect(CID, "S1_CID is required").toBeTruthy();
  await requireReliabilityStack(request);

  const notes: Array<Record<string, unknown>> = [];
  const frameLog = path.join(OUT, "stop-ws-frames.jsonl");
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

  async function capture(key: string, note: string) {
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(150);
    const probe = await page.evaluate(() => {
      const root = document.querySelector("[data-dr-phase]");
      const sig = document.querySelector("[data-dr-signal]");
      const checkpoint = document.querySelector('[data-dr-checkpoint="true"]');
      const controls = [...document.querySelectorAll("[data-disco-control]")].map((el) =>
        el.getAttribute("data-disco-control"),
      );
      return {
        phase: root?.getAttribute("data-dr-phase") ?? null,
        signal: sig?.getAttribute("data-dr-signal") ?? null,
        checkpointText: (checkpoint as HTMLElement | null)?.innerText ?? null,
        controls,
        alerts: [...document.querySelectorAll('[role="alert"]')].map(
          (el) => (el as HTMLElement).innerText,
        ),
      };
    });
    await page.screenshot({ path: path.join(OUT, `${key}.png`) });
    notes.push({ file: `${key}.png`, at: new Date().toISOString(), note, frameIndex: frameCount, ...probe });
    fs.writeFileSync(path.join(OUT, "stop-captures.json"), JSON.stringify(notes, null, 2), "utf-8");
  }

  await page.goto(`/deep/${CID}`);
  await page.locator("[data-dr-phase]").first().waitFor({ state: "attached", timeout: 60_000 });
  await page.waitForTimeout(3_000);
  await capture("80-before-stop", "the run live, immediately before the user presses Stop");

  await page.locator('[data-disco-control="dr.stop"]').first().click();
  await page.waitForTimeout(3_000);
  await capture("81-just-after-stop", "3 s after Stop — what the user sees while the loop unwinds");

  await page
    .locator('[data-dr-checkpoint="true"]')
    .waitFor({ state: "visible", timeout: 420_000 })
    .catch(() => {});
  await page.waitForTimeout(1_500);
  await capture("82-stopped-checkpoint", "the stopped state: checkpoint, sources retained, Resume available");

  const events = await allEvents(request, CID);
  fs.writeFileSync(path.join(OUT, "stop-events.json"), JSON.stringify({ cid: CID, events }, null, 2), "utf-8");
  fs.writeFileSync(path.join(OUT, "stop-captures.json"), JSON.stringify(notes, null, 2), "utf-8");
});
