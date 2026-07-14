import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-04 rung 4 (live, inspection-only — DRIVES NO BUILD): the behavioral run (a build
 * that creates index.html, serves it, then uses the `browser` tool to look at it on
 * http://127.0.0.1:8000/) rendered through the real UI. The feed must show the
 * browser action row ("Opened http://127.0.0.1:8000/" — buildTrace plainLabel) and
 * its expandable must carry the daemon's FENCED observation: the untrusted-content
 * fence + TITLE:. Under the old one-shot fetcher there was no daemon, no console
 * channel, no element index; the fence + TITLE here come from the rendered
 * Playwright page.
 *
 * DEVIATION vs the order text ("TITLE: and CONSOLE"): the landed renderer emits a
 * CONSOLE block only when error/warning entries exist — a clean page has none. The
 * structured console list is asserted on the EVENT payload instead (always carried).
 *
 * Target conversation: $PMX_SPEC_CID pin (the rung-4 behavioral run) or a scan of
 * known ids for a FINISHED build with a qualifying browser observation.
 *
 * Evidence: test-record/screenshots/bp-04/feed-browser-observation.png +
 *           test-record/bp-04/browser-observation-witness.json
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-04");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-04");

type ToolCall = { tool_name?: string; arguments?: Record<string, unknown>; call_id?: string };
type ToolResult = {
  tool_name?: string;
  call_id?: string;
  success?: boolean;
  content?: string;
  structured?: Record<string, unknown> | null;
  error?: string;
};
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

/** Ground truth a conversation must satisfy to witness BP-04 in the UI: a successful
 * browser observation on 127.0.0.1:8000 whose content is fenced with TITLE: and whose
 * structured payload has the daemon shape (console + elements lists). */
function browserWitness(events: EventJson[]) {
  const actions = events.filter(
    (e) => e.kind === "action" && e.tool_call?.tool_name === "browser",
  );
  const callIds = new Set(actions.map((a) => a.tool_call?.call_id).filter(Boolean));
  let obs: EventJson | undefined;
  for (const e of events) {
    if (e.kind !== "observation") continue;
    const tr = e.tool_result;
    if (!tr?.success || !callIds.has(tr.call_id)) continue;
    const st = (tr.structured ?? {}) as Record<string, unknown>;
    const url = String(st.url ?? "");
    if (!url.startsWith("http://127.0.0.1:8000") && !url.startsWith("http://localhost:8000"))
      continue;
    const content = tr.content ?? "";
    if (!content.includes("[UNTRUSTED WEB CONTENT") || !content.includes("TITLE:")) continue;
    if (!Array.isArray(st.console) || !Array.isArray(st.elements)) continue;
    obs = e;
    break;
  }
  return { actions, obs, ok: actions.length >= 1 && obs !== undefined };
}

test("feed renders the browser daemon observation (fence + TITLE) for the live build", async ({
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
  let witness: ReturnType<typeof browserWitness> | undefined;
  if (cid) {
    witness = browserWitness(await allEvents(request, cid));
    expect(witness.ok, `pinned ${cid} lacks a qualifying browser observation on :8000`)
      .toBeTruthy();
  } else {
    const known: string[] =
      (await fetchJson(request, `${API}/conversations?limit=25`)).conversation_ids ?? [];
    for (const candidate of known) {
      const state = await fetchJson(request, `${API}/conversations/${candidate}/state`);
      if (state.execution_status !== "FINISHED") continue;
      const w = browserWitness(await allEvents(request, candidate));
      if (w.ok) {
        cid = candidate;
        witness = w;
        break;
      }
    }
    test.skip(
      !witness?.ok,
      "no FINISHED conversation among the known 25 has a qualifying browser observation",
    );
  }
  const st = (witness!.obs!.tool_result!.structured ?? {}) as Record<string, unknown>;

  // 3. Open the build through the real UI (Resume route) and wait for feed rows.
  //    Tall viewport: the feed autoscrolls on live refetch, which races any
  //    scroll-dependent screenshot — with the whole feed in view nothing moves.
  await page.setViewportSize({ width: 1280, height: 2200 });
  await page.goto(`/build/${cid}`, { timeout: 30_000 });
  const rows = page.locator("button", { has: page.locator("span.truncate") });
  await expect(rows.first(), "no feed rows rendered").toBeVisible({ timeout: 30_000 });

  // 4. Feed: the browser action renders as "Opened <url>" (buildTrace plainLabel).
  const openedRow = page.getByText(/^Opened http:\/\/(127\.0\.0\.1|localhost):8000/, {
    exact: false,
  });
  await expect.poll(() => openedRow.count(), { timeout: 15_000 }).toBeGreaterThanOrEqual(1);

  // 5. Expand rows until the observation's fenced content appears in an Output <pre>.
  //    dispatchEvent, not click(): the feed's sticky header (plan progress, z-10)
  //    intercepts pointer events for rows scrolled beneath it (bp-06 lesson).
  const outputPre = page.locator("pre", { hasText: "[UNTRUSTED WEB CONTENT" });
  let expanded = (await outputPre.count()) > 0;
  const total = await rows.count();
  for (let i = 0; i < total && !expanded; i++) {
    await rows.nth(i).dispatchEvent("click");
    expanded = (await outputPre.count()) > 0;
  }
  expect(expanded, "no expanded row shows the fenced browser observation").toBeTruthy();
  const preText = (await outputPre.first().textContent()) ?? "";
  expect(preText, "fenced observation lost TITLE:").toContain("TITLE:");
  expect(preText, "fenced observation lost the URL line").toMatch(
    /URL: http:\/\/(127\.0\.0\.1|localhost):8000/,
  );

  // 6. Evidence. Two shots: the app viewport for context (plan + live preview +
  //    the expanded observation, all in the tall frame), and an ELEMENT shot of
  //    the fenced observation pre for close-up proof.
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  fs.mkdirSync(RECORD_DIR, { recursive: true });
  await page.screenshot({
    path: path.join(SCREENSHOT_DIR, "feed-browser-observation.png"),
  });
  await outputPre.first().screenshot({
    path: path.join(SCREENSHOT_DIR, "feed-browser-observation-expanded.png"),
  });
  fs.writeFileSync(
    path.join(RECORD_DIR, "browser-observation-witness.json"),
    JSON.stringify(
      {
        cid,
        browserActions: witness!.actions.length,
        obsSeq: witness!.obs!.seq,
        urlInStructured: st.url,
        consoleEntries: Array.isArray(st.console) ? st.console.length : null,
        elementEntries: Array.isArray(st.elements) ? st.elements.length : null,
        screenshotPath: st.screenshot_path ?? null,
        fenceVisibleInFeed: preText.includes("[UNTRUSTED WEB CONTENT"),
        titleVisibleInFeed: preText.includes("TITLE:"),
      },
      null,
      2,
    ),
  );
});
