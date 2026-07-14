import { expect, test, type APIRequestContext } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-13 UI rung (live, DRIVES A BUILD): suspended-sandbox badge + reconnect resume
 * through the real UI.
 *
 * Path under test: a clean FINISH snapshots + tears down the sandbox
 * (runtime._run_with_persistence → _maybe_snapshot → _teardown_sandbox), which is
 * the same `_suspend` state the idle-TTL sweep produces — so this rung witnesses
 * the badge contract (`state.extras.sandbox === "suspended"` → pill) without
 * needing to compress PMX_IDLE_SUSPEND_S on the live server. The follow-up
 * message then exercises the reconnect-resume chain LIVE: suspended → fresh
 * sandbox via SandboxSession._ensure() → workspace rehydrated → the agent can
 * extend a file it built BEFORE the suspend (the run-3 production defect was
 * exactly this chain dropping the workspace: "all files were lost").
 *
 * Evidence: test-record/screenshots/bp-13/suspend-badge.png + suspend-resumed.png
 *           test-record/bp-13/suspend-ui-witness.json
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-13");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-13");

const PROMPT =
  "Build a small static status page: an index.html with a heading 'Build Status', " +
  "a styles.css giving it a dark theme, and a data.json with three sample service " +
  "entries rendered into a list by a script.js. Serve it and verify it, then finish.";

const FOLLOW_UP =
  "Add a fourth service entry named 'metrics' with status 'degraded' to the " +
  "existing data.json (do not rebuild the other files — they already exist), " +
  "verify the page still renders all four entries, then finish.";

// Long polling loops hit two real-world hazards: uvicorn recycling idle
// keep-alive sockets ("socket hang up"), and the agent-server stalling for
// 45-60s while the in-sandbox browser-verify path runs (observed live in
// bp-12 run 3). Retry generously — total tolerance ≈ 5×(30s + 3s) ≈ 2.7 min.
interface WireEvent {
  kind?: string;
}

interface ConversationState {
  execution_status: string;
  extras?: { sandbox?: string };
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

async function status(request: APIRequestContext, cid: string): Promise<string> {
  return (await fullState(request, cid)).execution_status;
}

async function events(request: APIRequestContext, cid: string): Promise<WireEvent[]> {
  const body = await getJson<{ events?: WireEvent[] } | WireEvent[]>(
    request,
    `${API}/conversations/${cid}/events`,
  );
  return Array.isArray(body) ? body : (body.events ?? []); // tolerate either wire shape
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

test("clean finish suspends the sandbox (badge) → follow-up rehydrates and continues (live)", async ({
  page,
  request,
}) => {
  test.setTimeout(1_800_000); // two live builds on the local 27B back-to-back

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
      data: { surface: "build", title: "BP-13 UI rung: suspend badge + rehydrate" },
    })
  ).json();
  const cid: string = conv.conversation_id;

  await page.setViewportSize({ width: 1280, height: 2200 }); // bp-04 lesson: no autoscroll under shots
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  // 3. Prompt + approve the plan, run to FINISHED.
  const composer = page.getByPlaceholder(/plan a change to this build/i);
  await composer.fill(PROMPT, { timeout: 30_000 });
  await composer.press("Enter");
  await page.getByRole("button", { name: /approve\s*&\s*build/i }).click({ timeout: 300_000 });

  let st = await waitForStatus(request, cid, ["FINISHED", "ERROR"], 900_000);
  expect(st, `first build ended ${st}`).toBe("FINISHED");
  await expect(page.getByText("Finished", { exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });

  // 4. Clean FINISH → snapshot + teardown → state overlay must report the
  //    sandbox suspended. Poll the HTTP state (the overlay is computed at
  //    send time from runtime.sandbox_state, so it shows up on the next read).
  const suspendDeadline = Date.now() + 120_000;
  let sandbox: string | undefined;
  while (Date.now() < suspendDeadline) {
    sandbox = (await fullState(request, cid)).extras?.sandbox;
    if (sandbox === "suspended") break;
    await new Promise((r) => setTimeout(r, 3_000));
  }
  expect(sandbox, "state.extras.sandbox never reported 'suspended' after FINISH").toBe(
    "suspended",
  );

  // 5. Badge truth in the UI: the suspended pill renders next to the
  //    isolation-tier pill in AgentStatusBar. Reload so the freshly-overlaid
  //    state frame definitely reaches the page (WS state frames are only
  //    pushed on events; a reload re-requests state unconditionally).
  await page.reload({ timeout: 30_000 });
  const suspendedPill = page.getByText(/suspended/i).first();
  await expect(suspendedPill, "suspended badge not visible after sandbox teardown").toBeVisible({
    timeout: 60_000,
  });
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "suspend-badge.png") });

  // Fingerprint before the follow-up: actions so far, and the built artifact's
  // existence is implied by FINISHED + the bp-12-style event shape.
  const preEvents = await events(request, cid);
  const preActions = preEvents.filter((e) => e.kind === "action").length;

  // 6. Reconnect resume: a follow-up message against the SUSPENDED conversation.
  //    The sandbox must be recreated AND the workspace rehydrated — the task is
  //    only completable if data.json (built before the suspend) is still there.
  await composer.fill(FOLLOW_UP, { timeout: 30_000 });
  await composer.press("Enter");
  // Follow-ups on the build surface re-enter planning → approve again.
  await page.getByRole("button", { name: /approve\s*&\s*build/i }).click({ timeout: 300_000 });

  // 7. While RUNNING, the badge must NOT claim suspended (active overlay).
  st = await waitForStatus(request, cid, ["RUNNING", "FINISHED"], 180_000, 2_000);
  expect(["RUNNING", "FINISHED"], `follow-up never started (status ${st})`).toContain(st);
  if (st === "RUNNING") {
    const midState = await fullState(request, cid);
    expect(
      midState.extras?.sandbox,
      "state still claims 'suspended' while the follow-up is RUNNING",
    ).not.toBe("suspended");
  }

  st = await waitForStatus(request, cid, ["FINISHED", "ERROR"], 900_000);
  expect(st, `follow-up ended ${st}`).toBe("FINISHED");
  await expect(page.getByText("Finished", { exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "suspend-resumed.png") });

  // 8. Rehydration truth: the follow-up did MORE work (actions grew) and the
  //    workspace survived — the run-3 production failure mode announced itself
  //    as "The sandbox got recreated and all files were lost"; assert no such
  //    confession appears anywhere post-suspend.
  const postEvents = await events(request, cid);
  const postActions = postEvents.filter((e) => e.kind === "action").length;
  expect(postActions, "no further actions after the suspended follow-up").toBeGreaterThan(
    preActions,
  );
  const lostWorkspace = postEvents.some((e) =>
    JSON.stringify(e).toLowerCase().includes("all files were lost"),
  );
  expect(lostWorkspace, "agent observed a lost workspace — rehydrate chain broken").toBe(false);

  fs.mkdirSync(RECORD_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(RECORD_DIR, "suspend-ui-witness.json"),
    JSON.stringify(
      { cid, preActions, postActions, suspendedSeen: sandbox === "suspended", status: st },
      null,
      2,
    ),
  );
});
