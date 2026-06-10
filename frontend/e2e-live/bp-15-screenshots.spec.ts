import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-15 UI rung (live, DRIVES A BUILD): screenshots inline in the feed +
 * isolation tier over the wire.
 *
 * Paths under test: (1) the bp-05 verify gate forces every build through the
 * browser, so a screenshot observation ALWAYS lands in the feed — the feed
 * must render it as a real <img> thumbnail served by the new workspace route
 * (natural size > 0 = actually decoded PNG bytes, not a broken icon); (2) the
 * status-bar isolation pill must show the WIRE-reported backend tier. This
 * server runs gvisor — the retired hardcode said "local container" (visible
 * in bp-14's evidence shots), so asserting the gVisor label here proves the
 * hardcode is gone, not just re-labeled. (3) bp-13 composition: after FINISH
 * the sandbox suspends → the workspace route 404s. Two distinct truths there:
 * a same-context reload keeps SHOWING the thumbnail (the immutable cache, by
 * design — run 1 proved reload can't witness the 404), while a COLD-cache
 * context (fresh visitor) must degrade to the muted "screenshot no longer
 * available" placeholder, never a broken image.
 *
 * Evidence: test-record/screenshots/bp-15/feed-thumbnail.png +
 *           tier-badge.png + thumbnail-suspended.png
 *           test-record/bp-15/screenshots-ui-witness.json
 */

const API = "http://127.0.0.1:8000";
const BASE_URL = "http://localhost:5174"; // must match playwright.live.config.ts
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-15");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-15");

const PROMPT =
  "Build a tiny static page: index.html with the heading 'Screenshot Rung' and " +
  "a style.css making the background dark. Serve it, verify the page renders " +
  "in the browser, then finish.";

// bp-12 template hazards: uvicorn keep-alive recycling + 45-60s server stalls
// while in-sandbox browser-verify runs. Retry generously.
async function getJson(request: any, url: string): Promise<any> {
  let lastErr: unknown;
  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      return await (await request.get(url, { timeout: 30_000 })).json();
    } catch (err) {
      lastErr = err;
      await new Promise((r) => setTimeout(r, 3_000));
    }
  }
  throw lastErr;
}

async function fullState(request: any, cid: string): Promise<any> {
  return getJson(request, `${API}/conversations/${cid}/state`);
}

async function waitForStatus(
  request: any,
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

test("feed renders real screenshot thumbnails; tier badge reports the wire backend (live)", async ({
  page,
  request,
  browser,
}) => {
  test.setTimeout(1_500_000); // one live build on the local 27B

  // 1. Backend health — skip, never crash, when the agent-server is down.
  let healthy = false;
  try {
    healthy = (await request.get(`${API}/health`, { timeout: 5_000 })).ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start it and re-run`);

  // 2. Fresh conversation — the CREATE response itself must carry the backend.
  const conv = await (
    await request.post(`${API}/conversations`, {
      data: { surface: "build", title: "BP-15 UI rung: feed thumbnails + tier" },
    })
  ).json();
  const cid: string = conv.conversation_id;
  expect(conv.sandbox_backend, "create response missing sandbox_backend").toBe("gvisor");

  await page.setViewportSize({ width: 1280, height: 2200 }); // bp-04 lesson: no autoscroll under shots
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  // 3. Tier badge truth (wire, not hardcode): this server is gvisor-backed.
  //    The retired hardcode said "local container" — its absence is the point.
  const tierPill = page.getByText(/gVisor/i).first();
  await expect(tierPill, "gVisor tier badge not visible").toBeVisible({ timeout: 60_000 });
  await expect(
    page.getByText(/local container/i),
    "the old hardcoded 'local container' tier copy is still rendering",
  ).toHaveCount(0);
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "tier-badge.png") });

  // HTTP/WS parity (bp-13 invariant): the HTTP state must agree.
  const st0 = await fullState(request, cid);
  expect(st0.sandbox_backend, "HTTP /state missing sandbox_backend").toBe("gvisor");

  // 4. Prompt + approve, run to FINISHED. The bp-05 verify gate guarantees at
  //    least one browser observation → at least one screenshot in the feed.
  const composer = page.getByPlaceholder(/plan a change to this build/i);
  await composer.fill(PROMPT, { timeout: 30_000 });
  await composer.press("Enter");
  await page.getByRole("button", { name: /approve\s*&\s*build/i }).click({ timeout: 300_000 });

  // 5. Thumbnail truth, while the sandbox is still alive: wait for a feed
  //    <img> whose src points at the workspace route, then require it DECODED
  //    (naturalWidth > 0 — a 404'd or garbage response never decodes).
  const thumb = page
    .locator('img[src*="/workspace/.pmx/screenshots/"]')
    .first();
  await expect(thumb, "no screenshot thumbnail appeared in the feed").toBeVisible({
    timeout: 600_000,
  });
  await expect
    .poll(async () => thumb.evaluate((el: HTMLImageElement) => el.naturalWidth), {
      message: "thumbnail <img> present but never decoded (naturalWidth stayed 0)",
      timeout: 30_000,
    })
    .toBeGreaterThan(0);
  const naturalWidth = await thumb.evaluate((el: HTMLImageElement) => el.naturalWidth);
  const thumbSrc = await thumb.getAttribute("src");
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "feed-thumbnail.png") });

  // Route hardening spot-checks while the sandbox is live: traversal and
  // non-allowlisted paths must 404 (never 403 — don't confirm existence).
  const base = `${API}/conversations/${cid}/workspace`;
  for (const bad of [".pmx/screenshots/../../etc/passwd", "index.html", ".pmx/secrets"]) {
    const res = await request.get(`${base}/${bad}`, { timeout: 30_000 });
    expect(res.status(), `non-allowlisted path '${bad}' must 404`).toBe(404);
  }

  const st = await waitForStatus(request, cid, ["FINISHED", "ERROR"], 900_000);
  expect(st, `build ended ${st}`).toBe("FINISHED");

  // 6. bp-13 composition: FINISH → suspend → workspace files gone, route 404s.
  const suspendDeadline = Date.now() + 120_000;
  let sandbox: string | undefined;
  while (Date.now() < suspendDeadline) {
    sandbox = (await fullState(request, cid)).extras?.sandbox;
    if (sandbox === "suspended") break;
    await new Promise((r) => setTimeout(r, 3_000));
  }
  expect(sandbox, "sandbox never reported suspended after FINISH").toBe("suspended");
  const deadThumb = await request.get(`${API}${new URL(thumbSrc!, API).pathname}`);
  expect(deadThumb.status(), "workspace route must 404 once suspended").toBe(404);

  // 6a. Same-context reload: the IMMUTABLE cache keeps the thumbnail visible
  //     even though the route 404s — by design (write-once files, max-age=1y).
  //     The user who saw the screenshot keeps seeing it. Pin that.
  await page.reload({ timeout: 30_000 });
  await expect(
    page.locator('img[src*="/workspace/.pmx/screenshots/"]').first(),
    "immutable-cached thumbnail should survive a same-context reload",
  ).toBeVisible({ timeout: 120_000 });

  // 6b. COLD-cache visitor (fresh context = open the build later / elsewhere):
  //     the img request reaches the 404ing route → onError → the placeholder
  //     copy, never a broken-image icon. This is the degradation path the
  //     order's placeholder exists for — unreachable from a warm cache.
  const coldCtx = await browser.newContext({
    baseURL: BASE_URL,
    viewport: { width: 1280, height: 2200 },
  });
  const coldPage = await coldCtx.newPage();
  await coldPage.goto(`/build/${cid}`, { timeout: 30_000 });
  await expect(
    coldPage.getByText(/screenshot no longer available/i).first(),
    "suspended-sandbox placeholder copy not shown for dead thumbnails (cold cache)",
  ).toBeVisible({ timeout: 120_000 });
  // Tier badge must hold in the fresh context too (state frame, not a lucky cache).
  await expect(coldPage.getByText(/gVisor/i).first()).toBeVisible({ timeout: 30_000 });
  await coldPage.screenshot({ path: path.join(SCREENSHOT_DIR, "thumbnail-suspended.png") });
  await coldCtx.close();

  fs.mkdirSync(RECORD_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(RECORD_DIR, "screenshots-ui-witness.json"),
    JSON.stringify(
      {
        cid,
        createBackend: conv.sandbox_backend,
        stateBackend: st0.sandbox_backend,
        thumbSrc,
        naturalWidth,
        suspendedSeen: sandbox === "suspended",
        deadThumbStatus: deadThumb.status(),
        coldCachePlaceholderSeen: true, // assertion above gates reaching this line
        status: st,
      },
      null,
      2,
    ),
  );
});
