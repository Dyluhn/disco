/**
 * A live run survives a reload.
 *
 * A run started from `/` used to leave the address bar at `/`, so any reload —
 * a user's F5, a vite HMR full reload — landed back on the composer while the
 * engine kept working. F1's e2e-live rerun died exactly that way: the page
 * snapshot at the deadline was the composer while the server was still calling
 * the model. This drives the real thing: start a run, reload mid-run, and the
 * run view has to be back with its live heartbeat and its trace.
 *
 * Deliberately does NOT wait for a report — the point is the middle of the run.
 */
import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

import { conversationIdFromWebSocket, requireReliabilityStack } from "./reliability-helpers";

const OUT = path.join("e2e", "_artifacts", "f2-url-reload");
const QUESTION =
  "What is the measured state of sodium-ion battery cycle life in 2026, with " +
  "specific primary sources?";

test("a live run keeps its URL, and a reload returns to it", async ({ page, request }) => {
  test.setTimeout(900_000);
  await requireReliabilityStack(request);

  let cid = "";
  page.on("websocket", (socket) => {
    cid ||= conversationIdFromWebSocket(socket.url()) ?? "";
  });

  await page.goto("/");
  await page.getByRole("radio", { name: /deep research/i }).click();
  const input = page.getByPlaceholder(/ask a research question/i);
  await input.waitFor({ state: "visible", timeout: 30_000 });
  await page.locator('[data-disco-control="dr.options"]').click();
  await page.locator('[data-disco-control="dr.depth-tier"]').click();
  await page.getByRole("menuitem", { name: /^Quick/ }).click();
  await page.keyboard.press("Escape");
  await input.fill(QUESTION);
  await input.press("Enter");

  // The brief is the run's first visible output, so this proves the engine
  // really started before anything is claimed about the URL.
  await page.locator("[data-dr-brief]").waitFor({ state: "visible", timeout: 300_000 });
  expect(cid, "the Deep Research WebSocket did not identify its conversation").toBeTruthy();
  await expect(page).toHaveURL(new RegExp(`/deep/${cid}$`));

  // Let the trace grow past the brief so the reload has something to lose.
  const traceRows = page.locator("[data-dr-phase] ol > li");
  await expect.poll(async () => traceRows.count(), { timeout: 300_000 }).toBeGreaterThan(2);
  const before = await traceRows.count();
  await expect(page.locator('[data-dr-phase="running"]')).toBeVisible();
  await page.screenshot({ path: path.join(OUT, "01-before-reload.png"), fullPage: true });

  // The two screenshots look alike by design — that IS the result — so record
  // what a picture cannot show: the URL either side, and the browser's own
  // navigation type, which is what proves the page really reloaded.
  const probe = async (label: string) => ({
    label,
    url: page.url(),
    navigation: await page.evaluate(
      () =>
        (performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined)
          ?.type ?? "unknown",
    ),
    traceRows: await traceRows.count(),
    phase: await page.locator("[data-dr-phase]").first().getAttribute("data-dr-phase"),
    signal: await page.locator("[data-dr-signal]").first().getAttribute("data-dr-signal"),
  });
  const captures = [await probe("before-reload")];

  // A marker on the window object: it survives any in-page navigation and dies
  // with the document, so its absence afterwards is proof the page really was
  // replaced. (Firefox reports `navigation.type` as "navigate" for a driven
  // reload, so that field is recorded but cannot carry the assertion.)
  await page.evaluate(() => {
    (window as unknown as { __f2BeforeReload?: number }).__f2BeforeReload = Date.now();
  });

  await page.reload();

  // Back on the run, not the composer: no question box, the run's own phase,
  // a live heartbeat, and a trace at least as long as the one it replaced.
  await expect(page.locator('[data-dr-phase="running"]')).toBeVisible({ timeout: 120_000 });
  await expect(page.getByPlaceholder(/ask a research question/i)).toHaveCount(0);
  await expect(page.locator("[data-dr-brief]")).toBeVisible();
  await expect(page.locator("[data-dr-signal]")).toBeVisible();
  await expect.poll(async () => traceRows.count(), { timeout: 120_000 }).toBeGreaterThanOrEqual(
    before,
  );
  await expect(page).toHaveURL(new RegExp(`/deep/${cid}$`));
  await page.screenshot({ path: path.join(OUT, "02-after-reload.png"), fullPage: true });

  captures.push(await probe("after-reload"));
  const survivor = await page.evaluate(
    () => (window as unknown as { __f2BeforeReload?: number }).__f2BeforeReload ?? null,
  );
  expect(survivor, "the document was not replaced — this was not a reload").toBeNull();
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(
    path.join(OUT, "reload-captures.json"),
    JSON.stringify({ cid, captures }, null, 2),
    "utf-8",
  );

  // Stop the run rather than leaving the engine burning hosted calls.
  await page.locator('[data-disco-control="dr.kill"]').click();
});
