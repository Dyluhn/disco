import { expect, test, type APIRequestContext } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * DC-01 UI rung: Hostname-based preview proxy.
 *
 * Drives a real conversation that builds the bp-16 frozen scenario shape
 * (Vite+React app on :8000 reading `/api/readings` from :3000); asserts the
 * preview iframe's document contains REAL table cells with the CSV values.
 */

const API = "http://127.0.0.1:8000";
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
  return getJson<ConversationState>(request, `${API}/conversations/${cid}/state`);
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

test("Origin-true preview shows the Vite app and successfully fetches the API", async ({
  page,
  request,
}) => {
  test.setTimeout(1_500_000);

  let healthy = false;
  try {
    healthy = (await request.get(`${API}/health`, { timeout: 5_000 })).ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start it and re-run`);

  const conv = await (
    await request.post(`${API}/conversations`, {
      data: { surface: "build", title: "DC-01 UI rung: Hostname proxy" },
    })
  ).json();
  const cid: string = conv.conversation_id;

  await page.setViewportSize({ width: 1280, height: 2200 });
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });

  const composer = page.getByPlaceholder(/plan a change to this build/i);
  await composer.fill(PROMPT, { timeout: 30_000 });
  await composer.press("Enter");
  await page.getByRole("button", { name: /approve\s*&\s*build/i }).click({ timeout: 300_000 });

  // DC-01's surface is the proxy DURING a live run (post-FINISHED survival is
  // DC-02). Poll the origin-true hostnames directly until BOTH upstreams answer
  // through the proxy — that's the moment the servers are up and the run is
  // still holding (the prompt's `sleep 600` window).
  const cid8 = cid.replace(/^conv_/, "").slice(0, 8);
  const viteOrigin = `http://${cid8}-8000.localhost:8000`;
  const apiOrigin = `http://${cid8}-3000.localhost:8000`;
  await expect
    .poll(
      async () => {
        try {
          const [vite, api] = await Promise.all([
            request.get(`${viteOrigin}/`, { timeout: 10_000 }),
            request.get(`${apiOrigin}/api/readings`, { timeout: 10_000 }),
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
  const readings = await (await request.get(`${apiOrigin}/api/readings`)).json();
  expect(JSON.stringify(readings)).toContain("42");
  expect(JSON.stringify(readings)).toContain("84");

  // The pane DEFAULTS to the rendered (srcdoc) view when the agent wrote an
  // entry HTML — a Vite app always does — so the live iframe only appears
  // after the explicit "Live server" toggle (orchestrator fix: the draft
  // waited 600s for an iframe that could never render in rendered mode).
  // Mid-run the canvas follows the agent's activity (files/terminal) — the
  // PreviewPane lives behind the explicit "Preview" canvas tab.
  await page.getByRole("tab", { name: "Preview" }).click({ timeout: 30_000 });

  const liveToggle = page.getByRole("button", { name: /live server/i });
  const iframe = page.locator('iframe[title="Live preview"]');
  await expect
    .poll(
      async () => {
        if (await iframe.isVisible()) return true;
        if (await liveToggle.isVisible()) await liveToggle.click();
        return iframe.isVisible();
      },
      // Short: the upstreams already answer; only UI propagation is left, and
      // the run's sleep-window is ~600s total.
      { timeout: 60_000, intervals: [1_000] },
    )
    .toBe(true);

  const src = await iframe.getAttribute("src");
  expect(src).toMatch(/^http:\/\/[0-9a-f]{8}-8000\.localhost:8000\/\?r=\d+$/);

  const frame = iframe.contentFrame();
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

  // Port pill :3000 → the Express API through its own origin-true hostname.
  let port3000Src: string | null = null;
  const pill3000 = page.getByRole("tab", { name: ":3000" });
  if (await pill3000.isVisible()) {
    await pill3000.click();
    await expect(iframe).toHaveAttribute(
      "src",
      /^http:\/\/[0-9a-f]{8}-3000\.localhost:8000\/\?r=\d+$/,
      { timeout: 30_000 },
    );
    port3000Src = await iframe.getAttribute("src");
    await page.waitForTimeout(2_000);
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, "host-preview-port-3000.png") });
  }

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
      .poll(async () => (await request.get(`${viteOrigin}/`, { timeout: 10_000 })).status(), {
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
        iframeSrc: src,
        port3000Src,
        readings,
        finalStatus,
        postFinishProxyStatus: postFinishStatus,
      },
      null,
      2,
    ),
  );
});
