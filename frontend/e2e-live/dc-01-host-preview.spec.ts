import {
  expect,
  request as playwrightRequest,
  test,
  type APIRequestContext,
} from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  AGENT_API,
  authenticatedMutation,
  requireReliabilityStack,
} from "./reliability-helpers";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * DC-01 UI rung: canonical managed Preview.
 *
 * Drives a real conversation that builds the bp-16 frozen scenario shape
 * (Vite+React app on :8000 reading `/api/readings` from :3000); asserts the
 * canonical Preview iframe contains REAL table cells with the CSV values while
 * explicit host capabilities independently prove the two upstream ports.
 */

const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/dc-01");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/dc-01");

const PROMPT =
  "Create a Vite+React app on port 8000 and an Express API on port 3000. " +
  "Create a data.csv file with 'id,value\\n1,42\\n2,84'. " +
  "The API should read this CSV and serve it at /api/readings. " +
  "The React app should fetch from /api/readings (setup proxy in vite config) and display the data in a <table> with <td> cells. " +
  "Start both servers and verify them with curl. " +
  // Hold the run open: a clean FINISHED tears the sandbox down today (runtime.py
  // "G safe leak fix"), so the preview must be asserted DURING the run — keeping
  // it alive after FINISHED is DC-02's deliverable, not DC-01's.
  "Then run the command 'sleep 600' and wait for it to complete (call shell_wait as many times as needed) before finishing.";

interface ConversationState {
  execution_status: string;
}

interface PreviewState {
  available: boolean;
  generation?: string;
  port?: number | null;
  status?: string;
}

async function getJson<T>(request: APIRequestContext, url: string): Promise<T> {
  let lastErr: unknown;
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      return (await (await request.get(url, { timeout: 30_000 })).json()) as T;
    } catch (err) {
      lastErr = err;
      await new Promise((r) => setTimeout(r, 3_000));
    }
  }
  throw lastErr;
}

async function fullState(request: APIRequestContext, cid: string): Promise<ConversationState> {
  return getJson<ConversationState>(request, `${AGENT_API}/conversations/${cid}/state`);
}

async function waitForStatus(
  request: APIRequestContext,
  cid: string,
  wanted: string[],
  deadlineMs: number,
  pollMs = 5_000,
): Promise<string> {
  const deadline = Date.now() + deadlineMs;
  let st = "?";
  while (Date.now() < deadline) {
    st = (await fullState(request, cid)).execution_status;
    if (wanted.includes(st)) return st;
    await new Promise((r) => setTimeout(r, pollMs));
  }
  return st;
}

async function redeemHostPreview(
  request: APIRequestContext,
  cid: string,
  port: number,
): Promise<{ context: APIRequestContext; origin: string }> {
  const mint = await authenticatedMutation(
    request,
    `${AGENT_API}/conversations/${cid}/preview/capability`,
    {
      data: { port, target_path: "/", transport: "host" },
      timeout: 30_000,
    },
  );
  expect(mint.ok(), `host capability mint for :${port} returned ${mint.status()}`).toBe(true);
  const capability = (await mint.json()) as {
    bootstrap_url: string;
    bootstrap_intent: string;
    transport: string;
  };
  expect(capability.transport).toBe("host");
  const bootstrap = new URL(capability.bootstrap_url);
  expect(bootstrap.hostname).toMatch(
    new RegExp(`^p2-${cid.replace(/^conv_/, "").slice(0, 8)}-${port}\\.`),
  );
  expect(bootstrap.search).toBe("");
  expect(bootstrap.hash).toBe("");

  const isolated = await playwrightRequest.newContext();
  try {
    const redemption = await isolated.post(capability.bootstrap_url, {
      form: { intent: capability.bootstrap_intent },
      timeout: 30_000,
    });
    expect(redemption.status(), `host capability redemption for :${port} failed`).toBe(200);
    expect((await redemption.text()).includes(capability.bootstrap_intent)).toBe(false);
    return { context: isolated, origin: bootstrap.origin };
  } catch (error) {
    await isolated.dispose();
    throw error;
  }
}

test("Canonical Preview shows the Vite app and successfully fetches the API", async ({
  page,
  request,
}) => {
  test.setTimeout(1_500_000);

  let healthy = false;
  try {
    healthy = (await request.get(`${AGENT_API}/health`, { timeout: 5_000 })).ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${AGENT_API} — start it and re-run`);
  await requireReliabilityStack(request);

  const conversationResponse = await authenticatedMutation(
    request,
    `${AGENT_API}/conversations`,
    {
      data: { surface: "build", title: "DC-01 UI rung: Hostname proxy" },
    },
  );
  expect(conversationResponse.ok(), await conversationResponse.text()).toBe(true);
  const conv = await conversationResponse.json();
  const cid: string = conv.conversation_id;

  await page.setViewportSize({ width: 1280, height: 2200 });
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

  const composer = page.getByPlaceholder(/plan a change to this build/i);
  await composer.fill(PROMPT, { timeout: 30_000 });
  await composer.press("Enter");
  await page.getByRole("button", { name: /approve\s*&\s*build/i }).click({ timeout: 300_000 });

  // Poll explicit diagnostic capabilities until BOTH upstreams answer. The user
  // surface below remains the one canonical Preview selected by the platform.
  let vitePreviewForCleanup: Awaited<ReturnType<typeof redeemHostPreview>> | null = null;
  let apiPreviewForCleanup: Awaited<ReturnType<typeof redeemHostPreview>> | null = null;
  try {
  const vitePreview = await redeemHostPreview(request, cid, 8000);
  vitePreviewForCleanup = vitePreview;
  const apiPreview = await redeemHostPreview(request, cid, 3000);
  apiPreviewForCleanup = apiPreview;
  const viteOrigin = vitePreview.origin;
  const apiOrigin = apiPreview.origin;
  await expect
    .poll(
      async () => {
        try {
          const [vite, api] = await Promise.all([
            vitePreview.context.get(`${viteOrigin}/`, { timeout: 10_000 }),
            apiPreview.context.get(`${apiOrigin}/api/readings`, { timeout: 10_000 }),
          ]);
          const st = (await fullState(request, cid)).execution_status;
          if (["FINISHED", "ERROR", "IDLE"].includes(st)) {
            throw new Error(`run ended (${st}) before both upstreams answered`);
          }
          return vite.status() === 200 && api.status() === 200;
        } catch (err) {
          if (String(err).includes("run ended")) throw err;
          return false;
        }
      },
      { timeout: 900_000, intervals: [2_000] },
    )
    .toBe(true);

  // Proxy-level truth first: the Express origin serves the REAL csv rows.
  const readings = await (await apiPreview.context.get(`${apiOrigin}/api/readings`)).json();
  expect(JSON.stringify(readings)).toContain("42");
  expect(JSON.stringify(readings)).toContain("84");

  const preview = await getJson<PreviewState>(
    request,
    `${AGENT_API}/conversations/${cid}/preview`,
  );
  expect(preview).toMatchObject({ available: true, port: 8000, status: "running" });
  expect(preview.generation, "managed preview generation was absent").toBeTruthy();

  // Mid-run the canvas follows agent activity (files/terminal), so select the
  // single canonical Preview. There is no Rendered/Live representation toggle.
  await page.getByRole("tab", { name: "Preview" }).click({ timeout: 30_000 });
  const iframe = page.locator('iframe[title="Preview"]');
  await expect(iframe).toBeVisible({ timeout: 60_000 });

  const frame = iframe.contentFrame();
  await expect
    .poll(async () => {
      const handle = await iframe.elementHandle();
      return (await handle?.contentFrame())?.url() ?? "";
    })
    .toMatch(/^http:\/\/127(?:\.\d+){3}:\d+\//);
  const iframeHandle = await iframe.elementHandle();
  const iframeUrl = (await iframeHandle?.contentFrame())?.url() ?? "";
  expect(new URL(iframeUrl).origin).not.toBe(new URL(page.url()).origin);
  expect(new URL(iframeUrl).origin).not.toBe(viteOrigin);
  // Vite pre-bundles deps on the FIRST browser request; over the SSH->gVisor
  // transport that first paint can take tens of seconds (run 3 failed with the
  // module graph mid-load at the 60s mark). Be patient and nudge with the
  // pane's own Refresh if the iframe caught a mid-restart 502 page.
  const refreshBtn = page.getByRole("button", { name: /refresh/i });
  await expect
    .poll(
      async () => {
        if (await frame.getByRole("table").isVisible()) return true;
        if (await refreshBtn.isVisible()) await refreshBtn.click();
        return frame.getByRole("table").isVisible();
      },
      { timeout: 240_000, intervals: [5_000] },
    )
    .toBe(true);

  const cells = frame.getByRole("cell");
  // The CSV's real values — the exact preview_cells check bp-16 failed on.
  await expect(cells.filter({ hasText: "42" })).toHaveCount(1);
  await expect(cells.filter({ hasText: "84" })).toHaveCount(1);

  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "host-preview-table.png") });

  // Honest lifecycle coda: once the run cleanly FINISHES, today's runtime tears
  // the sandbox down and the proxy MUST answer 503 (not hang, not 502 noise).
  // DC-02 (idle-suspend + resume) is the order that flips this to survival.
  const finalStatus = await waitForStatus(
    request,
    cid,
    ["FINISHED", "ERROR", "IDLE", "PAUSED"],
    600_000,
  );
  let postFinishStatus: number | null = null;
  if (finalStatus === "FINISHED") {
    await expect
      .poll(async () => (await vitePreview.context.get(`${viteOrigin}/`, { timeout: 10_000 })).status(), {
        timeout: 60_000,
        intervals: [2_000],
      })
      .toBe(503);
    postFinishStatus = 503;
  }

  fs.mkdirSync(RECORD_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(RECORD_DIR, "host-preview-witness.json"),
    JSON.stringify(
      {
        cid,
        iframeUrl,
        previewGeneration: preview.generation,
        apiDiagnosticOrigin: apiOrigin,
        readings,
        finalStatus,
        postFinishProxyStatus: postFinishStatus,
      },
      null,
      2,
    ),
  );
  } finally {
    await Promise.all(
      [vitePreviewForCleanup, apiPreviewForCleanup]
        .filter((preview) => preview !== null)
        .map((preview) => preview.context.dispose()),
    );
  }
});
