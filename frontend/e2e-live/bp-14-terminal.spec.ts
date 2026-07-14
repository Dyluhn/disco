import { expect, test, type APIRequestContext } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-14 UI rung (live, DRIVES A BUILD): Terminal tab = live tmux session views.
 *
 * Path under test: while a build RUNS, the agent's shell work lives in real
 * tmux sessions inside the sandbox (BP-01 ShellSessionManager). The Terminal
 * tab must render those sessions LIVE — chips row + polled capture-pane
 * content — instead of (only) the derived event-history fallback. Internal
 * `__*` sessions (browser daemon, kernel) must never appear. After FINISH the
 * sandbox suspends (bp-13) → /sessions correctly reports [] → the tab falls
 * back to derived history with its muted caption.
 *
 * Evidence: test-record/screenshots/bp-14/terminal-live.png + terminal-history.png
 *           test-record/bp-14/terminal-ui-witness.json
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-14");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-14");

// The prompt deliberately makes the agent START A SERVER (long-lived shell
// session) and keep working afterwards, so the spec has a window where a
// session is alive + busy while the conversation is RUNNING.
const PROMPT =
  "Build a tiny static page: index.html with the heading 'Terminal Rung' and a " +
  "style.css making the background dark. Serve the directory with a web server, " +
  "verify the page renders in the browser, then finish.";

// bp-12 template hazards: uvicorn keep-alive recycling + 45-60s server stalls
// while in-sandbox browser-verify runs. Retry generously.
interface LiveSession {
  name: string;
  busy?: boolean;
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

async function status(request: APIRequestContext, cid: string): Promise<string> {
  return (
    await getJson<{ execution_status: string }>(
      request,
      `${API}/conversations/${cid}/state`,
    )
  ).execution_status;
}

async function sessions(request: APIRequestContext, cid: string): Promise<LiveSession[]> {
  const body = await getJson<{ sessions?: LiveSession[] }>(
    request,
    `${API}/conversations/${cid}/sessions`,
  );
  return body.sessions ?? [];
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
    st = await status(request, cid);
    if (wanted.includes(st)) return st;
    await new Promise((r) => setTimeout(r, pollMs));
  }
  return st;
}

test("Terminal tab shows live tmux sessions mid-build, history fallback after suspend (live)", async ({
  page,
  request,
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

  // 2. Fresh conversation on the build surface.
  const conv = await (
    await request.post(`${API}/conversations`, {
      data: { surface: "build", title: "BP-14 UI rung: live terminal sessions" },
    })
  ).json();
  const cid: string = conv.conversation_id;

  await page.setViewportSize({ width: 1280, height: 2200 }); // bp-04 lesson: no autoscroll under shots
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  // 3. Prompt + approve the plan.
  const composer = page.getByPlaceholder(/plan a change to this build/i);
  await composer.fill(PROMPT, { timeout: 30_000 });
  await composer.press("Enter");
  await page.getByRole("button", { name: /approve\s*&\s*build/i }).click({ timeout: 300_000 });

  // 4. Wire truth first: poll /sessions until a NON-internal session exists
  //    while the build is RUNNING. The agent's serve step keeps one busy.
  //    Also assert the route NEVER leaks `__*` names (order prohibition).
  const sessionDeadline = Date.now() + 600_000;
  let liveSessions: LiveSession[] = [];
  let sawBusy = false;
  let leakedInternal = false;
  while (Date.now() < sessionDeadline) {
    const st = await status(request, cid);
    const list = await sessions(request, cid);
    if (list.some((s) => String(s.name).startsWith("__"))) leakedInternal = true;
    if (list.length > 0) {
      liveSessions = list;
      sawBusy = sawBusy || list.some((s) => s.busy);
      // keep sampling briefly to catch busy=true, but don't wait forever
      if (sawBusy) break;
    }
    if (st === "FINISHED" || st === "ERROR") break;
    await new Promise((r) => setTimeout(r, 2_000));
  }
  expect(leakedInternal, "`__*` internal session leaked from /sessions").toBe(false);
  expect(liveSessions.length, "no live session ever appeared during the build").toBeGreaterThan(0);

  // 5. UI truth while sessions are live: open the Terminal tab → session chip
  //    visible + live pane has content; screenshot. (Selectors re-verified at
  //    diff review against the worker's TerminalPane implementation.)
  await page.getByRole("tab", { name: /terminal/i }).click({ timeout: 30_000 });
  const chipName = String(liveSessions[0].name);
  await expect(
    page.getByText(chipName, { exact: false }).first(),
    `session chip '${chipName}' not visible in the Terminal tab`,
  ).toBeVisible({ timeout: 60_000 });
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "terminal-live.png") });

  // Wire shape spot-check on /view for the first session.
  const view = await getJson<{ content?: unknown }>(
    request,
    `${API}/conversations/${cid}/sessions/${encodeURIComponent(chipName)}/view?tail_chars=10000`,
  );
  expect(typeof view.content, "/view did not return string content").toBe("string");

  // Internal sessions must 404 from /view even by direct request.
  const internal = await request.get(
    `${API}/conversations/${cid}/sessions/__browser/view`,
    { timeout: 30_000 },
  );
  expect(internal.status(), "/view on __browser must 404").toBe(404);

  // 6. Run to FINISHED → bp-13 suspends the sandbox → /sessions returns [].
  const st = await waitForStatus(request, cid, ["FINISHED", "ERROR"], 900_000);
  expect(st, `build ended ${st}`).toBe("FINISHED");
  const emptyDeadline = Date.now() + 120_000;
  let post: LiveSession[] = [{ name: "sentinel" }];
  while (Date.now() < emptyDeadline) {
    post = await sessions(request, cid);
    if (post.length === 0) break;
    await new Promise((r) => setTimeout(r, 3_000));
  }
  expect(post.length, "/sessions not empty after suspend").toBe(0);

  // 7. Fallback truth: reload, Terminal tab shows derived history with the
  //    muted caption — deriveTerminal must SURVIVE as documented fallback.
  await page.reload({ timeout: 30_000 });
  await page.getByRole("tab", { name: /terminal/i }).click({ timeout: 30_000 });
  await expect(
    page.getByText(/history \(no live sessions\)/i).first(),
    "history-fallback caption missing after suspend",
  ).toBeVisible({ timeout: 60_000 });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "terminal-history.png") });

  fs.mkdirSync(RECORD_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(RECORD_DIR, "terminal-ui-witness.json"),
    JSON.stringify(
      {
        cid,
        liveSessionNames: liveSessions.map((s) => s.name),
        sawBusy,
        leakedInternal,
        postSuspendSessions: post.length,
        status: st,
      },
      null,
      2,
    ),
  );
});
