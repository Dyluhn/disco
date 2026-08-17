import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-05 UI rung (live, inspection-only — DRIVES NO BUILD): the two behavioral runs
 * (test-record/bp-05/run_behavioral.py A|B) rendered through the real UI.
 *
 *  - CLEAN (run B): the feed shows the browser observation row ("Opened
 *    http://127.0.0.1:8000/") and the run reads Finished — the agent verified
 *    BEFORE finishing, zero refusals (asserted on the event log).
 *  - WARNED (run A): the agent shipped a seeded console.error and exhausted the
 *    3-refusal valve — the ⚠ "finished WITHOUT a clean browser verification"
 *    system_warning row must RENDER in the feed (environment meta is otherwise
 *    hidden; the valve is deliberately surfaced).
 *
 * Target conversations: $PMX_SPEC_CID_CLEAN / $PMX_SPEC_CID_WARNED pins, else a
 * scan of known ids for FINISHED builds matching each witness.
 *
 * Evidence: test-record/screenshots/bp-05/verify-clean-finish.png +
 *           verify-warned-finish.png + test-record/bp-05/verify-gate-witness.json
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-05");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-05");

const VALVE_PREFIX = "⚠ finished WITHOUT a clean browser verification";
const NUDGE_PREFIX = "Before finishing: verify your app the way a user would.";

type ToolCall = { tool_name?: string; arguments?: Record<string, unknown>; call_id?: string };
type ToolResult = {
  tool_name?: string;
  call_id?: string;
  success?: boolean;
  content?: string;
  structured?: Record<string, unknown> | null;
};
type EventJson = {
  seq?: number;
  kind?: string;
  source?: string;
  id?: string;
  tool_call?: ToolCall;
  tool_result?: ToolResult;
  message?: { content?: string } | null;
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

function envMessages(events: EventJson[]): string[] {
  return events
    .filter((e) => e.kind === "message" && e.source === "environment")
    .map((e) => e.message?.content ?? "");
}

function browserObsOn8000(events: EventJson[]): EventJson[] {
  const callIds = new Set(
    events
      .filter((e) => e.kind === "action" && e.tool_call?.tool_name === "browser")
      .map((a) => a.tool_call?.call_id)
      .filter(Boolean),
  );
  return events.filter((e) => {
    if (e.kind !== "observation") return false;
    const tr = e.tool_result;
    if (!tr?.success || !callIds.has(tr.call_id)) return false;
    const url = String((tr.structured ?? {}).url ?? "");
    return url.startsWith("http://127.0.0.1:8000") || url.startsWith("http://localhost:8000");
  });
}

/** Ground truth for the CLEAN run: clean browser obs, zero refusals, no valve. */
function cleanWitness(events: EventJson[]) {
  const env = envMessages(events);
  const obs = browserObsOn8000(events).filter((e) => {
    const console_ = (e.tool_result?.structured ?? {}).console;
    return (
      Array.isArray(console_) &&
      !console_.some((c) => (c as Record<string, unknown>).level === "error")
    );
  });
  return {
    obs,
    ok:
      obs.length >= 1 &&
      !env.some((m) => m.startsWith(NUDGE_PREFIX)) &&
      !env.some((m) => m.startsWith(VALVE_PREFIX)),
  };
}

/** Ground truth for the WARNED run: gate fired (nudges) and the ⚠ valve released. */
function warnedWitness(events: EventJson[]) {
  const env = envMessages(events);
  const nudges = env.filter((m) => m.startsWith(NUDGE_PREFIX));
  const valves = env.filter((m) => m.startsWith(VALVE_PREFIX));
  return { nudges, valves, ok: nudges.length >= 1 && valves.length === 1 };
}

async function resolveCid(
  request: import("@playwright/test").APIRequestContext,
  pinned: string | undefined,
  witness: (events: EventJson[]) => { ok: boolean },
): Promise<string | null> {
  if (pinned) {
    if (!witness(await allEvents(request, pinned)).ok)
      throw new Error(`pinned ${pinned} does not satisfy the witness`);
    return pinned;
  }
  const known: string[] =
    (await fetchJson(request, `${API}/conversations?limit=25`)).conversation_ids ?? [];
  for (const candidate of known) {
    const state = await fetchJson(request, `${API}/conversations/${candidate}/state`);
    if (state.execution_status !== "FINISHED") continue;
    if (witness(await allEvents(request, candidate)).ok) return candidate;
  }
  return null;
}

async function checkHealth(request: import("@playwright/test").APIRequestContext) {
  try {
    return (await request.get(`${API}/health`, { timeout: 5_000 })).ok();
  } catch {
    return false;
  }
}

test("clean run: feed shows the browser verification before Finished", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000);
  test.skip(!(await checkHealth(request)), `agent-server not reachable at ${API}`);

  const cid = await resolveCid(request, process.env.PMX_SPEC_CID_CLEAN, cleanWitness);
  test.skip(!cid, "no FINISHED conversation matches the clean-verified witness");

  // Tall viewport: the feed autoscrolls on live refetch — with everything in
  // view nothing moves under the screenshot (bp-04 lesson).
  await page.setViewportSize({ width: 1280, height: 2200 });
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  // The browser action renders as "Opened <url>" (buildTrace plainLabel)...
  const openedRow = page.getByText(/^Opened http:\/\/(127\.0\.0\.1|localhost):8000/);
  await expect.poll(() => openedRow.count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(1);
  // ...the run reads Finished (AgentStatusBar)...
  await expect(page.getByText("Finished", { exact: true }).first()).toBeVisible({
    timeout: 15_000,
  });
  // ...and NO ⚠ valve row exists anywhere in the feed.
  expect(await page.getByText(VALVE_PREFIX, { exact: false }).count()).toBe(0);

  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "verify-clean-finish.png") });
});

test("warned run: the ⚠ release-valve warning renders in the feed", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000);
  test.skip(!(await checkHealth(request)), `agent-server not reachable at ${API}`);

  const cid = await resolveCid(request, process.env.PMX_SPEC_CID_WARNED, warnedWitness);
  test.skip(!cid, "no FINISHED conversation matches the warned-finish witness");
  const witness = warnedWitness(await allEvents(request, cid!));

  await page.setViewportSize({ width: 1280, height: 2200 });
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  // The ⚠ system_warning row is REAL feed content (environment meta is hidden;
  // the valve is deliberately surfaced — buildTrace deriveActivity).
  const warnRow = page.getByText(VALVE_PREFIX, { exact: false });
  await expect.poll(() => warnRow.count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(1);
  await expect(page.getByText("Warning", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Finished", { exact: true }).first()).toBeVisible({
    timeout: 15_000,
  });

  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  fs.mkdirSync(RECORD_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "verify-warned-finish.png") });
  fs.writeFileSync(
    path.join(RECORD_DIR, "verify-gate-witness.json"),
    JSON.stringify(
      {
        warnedCid: cid,
        nudges: witness.nudges.length,
        valveText: witness.valves[0] ?? null,
        warnRowVisibleInFeed: (await warnRow.count()) >= 1,
      },
      null,
      2,
    ),
  );
});
