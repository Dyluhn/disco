import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-08 rung 5 (live, inspection-only — DRIVES NO BUILD): the rung-4 behavioral run
 * (cross-cell-state CSV analysis on the persistent kernel) rendered through the real
 * UI. The Terminal pane must show >= 3 separate `python «code»` cells with their
 * outputs — including the deterministic cross-cell value TOTAL=55, which under the
 * old pickle runner only survived by serialization accident and is now kernel RAM.
 * The feed must show the matching "Ran python code" rows, and the state-loss
 * signature (NameError) must appear nowhere.
 *
 * Target conversation: $PMX_SPEC_CID pin (the BP-08 rung-4 run) or a scan of known
 * ids for a FINISHED build with >= 3 code_exec actions and the 55 marker.
 *
 * Evidence: test-record/screenshots/bp-08/kernel-cells.png +
 *           test-record/bp-08/kernel-cells-witness.json
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-08");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-08");

type ToolCall = { tool_name?: string; arguments?: Record<string, unknown> };
type ToolResult = { tool_name?: string; content?: string; error?: string };
type EventJson = {
  seq?: number;
  kind?: string;
  action_id?: string;
  id?: string;
  tool_call?: ToolCall;
  tool_result?: ToolResult;
};

async function fetchJson(request: import("@playwright/test").APIRequestContext, url: string) {
  const res = await request.get(url, { timeout: 15_000 });
  if (!res.ok()) throw new Error(`GET ${url} -> ${res.status()}: ${await res.text()}`);
  return res.json();
}

/** Full paginated event log (limit=100 pages, ordered by seq). */
async function allEvents(
  request: import("@playwright/test").APIRequestContext,
  cid: string,
): Promise<EventJson[]> {
  const events: EventJson[] = [];
  let after = 0;
  for (let page = 0; page < 100; page++) {
    const body = await fetchJson(
      request,
      `${API}/conversations/${cid}/events?limit=100&after_seq=${after}`,
    );
    const batch: EventJson[] = body.events ?? [];
    if (batch.length === 0) break;
    events.push(...batch);
    after = Math.max(...batch.map((e) => e.seq ?? after));
  }
  return events;
}

/** Ground truth a conversation must satisfy to witness BP-08 in the UI. */
function kernelWitness(events: EventJson[]) {
  const cells = events.filter(
    (e) => e.kind === "action" && e.tool_call?.tool_name === "code_exec",
  );
  const blob = JSON.stringify(events);
  const hasMarker = blob.includes("TOTAL=55") || blob.includes("total=55");
  const hasNameError = blob.includes("NameError");
  return { cells, ok: cells.length >= 3 && hasMarker && !hasNameError };
}

test("Terminal + feed render the cross-cell kernel run; state visibly carried (live)", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000); // inspection-only: well under 5 minutes

  // 1. Backend health — skip, never crash, when the agent-server is down.
  let healthy = false;
  try {
    const res = await request.get(`${API}/health`, { timeout: 5_000 });
    healthy = res.ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start it and re-run`);

  // 2. Resolve the target conversation: env pin, else snapshot-of-known-ids scan.
  let cid = process.env.PMX_SPEC_CID ?? "";
  let witness: ReturnType<typeof kernelWitness> | undefined;
  if (cid) {
    witness = kernelWitness(await allEvents(request, cid));
    expect(witness.ok, `pinned ${cid} lacks >=3 clean code_exec cells + the 55 marker`)
      .toBeTruthy();
  } else {
    const known: string[] =
      (await fetchJson(request, `${API}/conversations?limit=25`)).conversation_ids ?? [];
    for (const candidate of known) {
      const state = await fetchJson(request, `${API}/conversations/${candidate}/state`);
      if (state.execution_status !== "FINISHED") continue;
      const w = kernelWitness(await allEvents(request, candidate));
      if (w.ok) {
        cid = candidate;
        witness = w;
        break;
      }
    }
    test.skip(
      !witness?.ok,
      "no FINISHED conversation among the known 25 has >=3 clean code_exec cells with the 55 marker",
    );
  }
  const cellCount = witness!.cells.length;

  // 3. Open the build through the real UI (Resume route) and wait for feed rows.
  await page.goto(`/build/${cid}`, { timeout: 30_000 });
  const rows = page.locator("button", { has: page.locator("span.truncate") });
  await expect(rows.first(), "no feed rows rendered").toBeVisible({ timeout: 30_000 });

  // 4. Feed: every code_exec action renders as a "Ran python code" row (buildTrace
  //    VERB map) — one per cell, so the multi-cell shape is user-visible at a glance.
  //    The label is a sibling of the expandable args button, not inside it, so match
  //    the text node itself rather than filtering the button rows.
  const codeRows = page.getByText("Ran python code", { exact: true });
  await expect
    .poll(() => codeRows.count(), { timeout: 15_000 })
    .toBeGreaterThanOrEqual(3);

  // 5. Terminal pane: each cell is a `$ python «code»` entry paired with its REAL
  //    output. The cross-cell proof is output text, not row count: TOTAL=55 was
  //    computed from `rows` loaded in an EARLIER cell, so it can only appear if the
  //    kernel carried state across cells.
  await page.getByRole("tab", { name: /terminal/i }).first().click();
  const cellHeaders = page.locator("span", { hasText: "python «code»" });
  await expect
    .poll(() => cellHeaders.count(), { timeout: 15_000 })
    .toBeGreaterThanOrEqual(3);
  const terminalText = (await page.locator("pre").allTextContents()).join("\n");
  expect(terminalText, "cross-cell value TOTAL=55 not visible in Terminal output").toMatch(
    /TOTAL=55|total=55/,
  );
  expect(terminalText, "state-loss signature leaked into the Terminal").not.toContain(
    "NameError",
  );

  // 6. Evidence.
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  fs.mkdirSync(RECORD_DIR, { recursive: true });
  await page.screenshot({
    path: path.join(SCREENSHOT_DIR, "kernel-cells.png"),
    fullPage: true,
  });
  fs.writeFileSync(
    path.join(RECORD_DIR, "kernel-cells-witness.json"),
    JSON.stringify(
      {
        cid,
        codeExecCells: cellCount,
        feedCodeRows: await codeRows.count(),
        terminalCells: await cellHeaders.count(),
        markerSeen: /TOTAL=55|total=55/.test(terminalText),
      },
      null,
      2,
    ),
  );
});
