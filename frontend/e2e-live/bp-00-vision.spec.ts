import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-00 UI rung (live, inspection-only — DRIVES NO BUILD): the behavioral run
 * (test-record/bp-00/run_behavioral.py — vision driver catches seeded
 * white-on-white, console clean) rendered through the real UI.
 *
 * DEVIATION from the order's step-4 wording, called out per process: the order
 * references the BP-15 feed screenshot THUMBNAIL, but BP-15 has not landed (it is
 * later in the campaign). This spec asserts what exists today instead: the browse
 * rows ("Opened …"), the subsequent fixing edit row ("Edited/Wrote …css…"), and a
 * READABLE final Preview (the served heading visible in the preview frame). The
 * thumbnail assertion moves to bp-15's spec when BP-15 lands.
 *
 * Evidence:
 *  - defect-before.png  — the agent's OWN first screenshot_b64 (the white-on-white
 *    page as the driver saw it), decoded from the event log;
 *  - fixed-after.png    — live Firefox screenshot of the Build surface with the
 *    fixed, readable Preview.
 *
 * Target conversation: $PMX_SPEC_CID_VISION pin, else a scan of known ids for a
 * FINISHED build matching the vision witness.
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-00");

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
  thought?: string;
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

function browserObsLocal(events: EventJson[]): EventJson[] {
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
    return url.startsWith("http://127.0.0.1") || url.startsWith("http://localhost");
  });
}

const VISUAL_RE =
  /white.{0,40}(text|background)|invisible|unreadable|blank|(can.?t|cannot|hard to) (see|read)|contrast|same colou?r|not visible/i;

/**
 * Ground truth mirrors run_behavioral.py: FINISHED build, >=1 screenshot_b64
 * observation, ZERO console errors, a post-screenshot visual-defect thought,
 * and a fixing css edit after it.
 */
function visionWitness(events: EventJson[]) {
  const obs = browserObsLocal(events);
  const shotObs = obs.filter((e) => (e.tool_result?.structured ?? {}).screenshot_b64);
  const errObs = obs.filter((e) => {
    const console_ = (e.tool_result?.structured ?? {}).console;
    return (
      Array.isArray(console_) &&
      console_.some((c) => (c as Record<string, unknown>).level === "error")
    );
  });
  const firstShotSeq = shotObs.length ? (shotObs[0].seq ?? 0) : null;
  const visualThought = events.find(
    (e) =>
      e.kind === "action" &&
      firstShotSeq !== null &&
      (e.seq ?? 0) > firstShotSeq &&
      VISUAL_RE.test(e.thought ?? ""),
  );
  // >= not >: an ActionEvent carries thought AND tool_call in ONE event, so an
  // identify-and-fix step shares a seq (mirrors run_behavioral.py's assertion).
  const fixingEdit = events.find(
    (e) =>
      e.kind === "action" &&
      visualThought !== undefined &&
      (e.seq ?? 0) >= (visualThought.seq ?? 0) &&
      /--fg|color|style\.css/i.test(JSON.stringify(e.tool_call?.arguments ?? {})),
  );
  return {
    shotObs,
    visualThought,
    fixingEdit,
    ok: shotObs.length >= 1 && errObs.length === 0 && !!visualThought && !!fixingEdit,
  };
}

async function resolveCid(
  request: import("@playwright/test").APIRequestContext,
  pinned: string | undefined,
): Promise<string | null> {
  if (pinned) {
    if (!visionWitness(await allEvents(request, pinned)).ok)
      throw new Error(`pinned ${pinned} does not satisfy the vision witness`);
    return pinned;
  }
  const known: string[] =
    (await fetchJson(request, `${API}/conversations?limit=25`)).conversation_ids ?? [];
  for (const candidate of known) {
    const state = await fetchJson(request, `${API}/conversations/${candidate}/state`);
    if (state.execution_status !== "FINISHED") continue;
    if (visionWitness(await allEvents(request, candidate)).ok) return candidate;
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

test("vision run: feed shows browse → visual fix; Preview is readable", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000);
  test.skip(!(await checkHealth(request)), `agent-server not reachable at ${API}`);

  const cid = await resolveCid(request, process.env.PMX_SPEC_CID_VISION);
  test.skip(!cid, "no FINISHED conversation matches the vision witness");
  const events = await allEvents(request, cid!);
  const witness = visionWitness(events);

  // defect-before.png: the white-on-white page exactly as the DRIVER saw it —
  // decoded from the first screenshot_b64 the daemon returned. This is the only
  // honest "before" (by spec-run time the live app is already fixed).
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  const b64 = String((witness.shotObs[0].tool_result?.structured ?? {}).screenshot_b64);
  fs.writeFileSync(path.join(SCREENSHOT_DIR, "defect-before.png"), Buffer.from(b64, "base64"));

  // Tall viewport: the feed autoscrolls on live refetch — with everything in
  // view nothing moves under the screenshot (bp-04 lesson).
  await page.setViewportSize({ width: 1280, height: 2200 });
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  // Browse rows render as "Opened <url>" (buildTrace plainLabel) — the run
  // browses at least twice: the defect look and the post-fix verification.
  const openedRow = page.getByText(/^Opened http:\/\/(127\.0\.0\.1|localhost)/);
  await expect.poll(() => openedRow.count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(2);

  // The fixing edit renders as "Edited/Wrote …" touching the stylesheet.
  const editRow = page.getByText(/^(Edited|Wrote) .*(css|html)/i);
  await expect.poll(() => editRow.count(), { timeout: 30_000 }).toBeGreaterThanOrEqual(1);

  // The run reads Finished (AgentStatusBar).
  await expect(page.getByText("Finished", { exact: true }).first()).toBeVisible({
    timeout: 15_000,
  });

  fs.writeFileSync(
    path.join(SCREENSHOT_DIR, "..", "..", "bp-00", "vision-ui-witness.json"),
    JSON.stringify(
      {
        cid,
        screenshotObs: witness.shotObs.length,
        visualThoughtSeq: witness.visualThought?.seq ?? null,
        visualThought: witness.visualThought?.thought ?? null,
        fixingEditSeq: witness.fixingEdit?.seq ?? null,
      },
      null,
      2,
    ),
  );
});

test("vision run: final Preview shows the heading readably", async ({ page, request }) => {
  test.setTimeout(300_000);
  test.skip(!(await checkHealth(request)), `agent-server not reachable at ${API}`);

  const cid = await resolveCid(request, process.env.PMX_SPEC_CID_VISION);
  test.skip(!cid, "no FINISHED conversation matches the vision witness");

  await page.setViewportSize({ width: 1280, height: 2200 });
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  // Open the Preview pane and assert the served page's heading is present in the
  // preview frame — i.e. the fix landed and the content is real, not blank.
  // (Readability — dark-on-white — is evidenced by fixed-after.png, sent to user.)
  await page.getByRole("tab", { name: /preview/i }).first().click({ timeout: 15_000 });
  const previewFrame = page.frameLocator("iframe").first();
  await expect(previewFrame.getByText("Build Status: GREEN")).toBeVisible({ timeout: 30_000 });

  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "fixed-after.png") });
});
