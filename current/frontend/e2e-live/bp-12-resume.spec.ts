import { expect, test, type APIRequestContext } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-12 UI rung (live, DRIVES A BUILD): first-class Resume through the real UI.
 * Start a build, approve the plan, let it do real work, click Stop (graceful,
 * cooperative), assert the Paused state + the Resume button, click Resume,
 * assert the run CONTINUES (RUNNING again, NO re-plan — same single PlanEvent
 * revision) and reaches FINISHED.
 *
 * Evidence: test-record/screenshots/bp-12/resume-paused.png + resume-running.png
 *           test-record/bp-12/resume-ui-witness.json
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-12");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-12");

const PROMPT =
  "Build a small static status page: an index.html with a heading 'Build Status', " +
  "a styles.css giving it a dark theme, and a data.json with three sample service " +
  "entries rendered into a list by a script.js. Serve it and verify it, then finish.";

// Long polling loops hit two real-world hazards: uvicorn recycling idle
// keep-alive sockets ("socket hang up"), and the agent-server stalling for
// 45-60s while the in-sandbox browser-verify path runs (observed live in
// bp-12 run 3). Retry generously — total tolerance ≈ 5×(30s + 3s) ≈ 2.7 min.
interface WireEvent {
  kind?: string;
  source?: string;
  message?: { content?: string };
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

async function state(request: APIRequestContext, cid: string): Promise<string> {
  return (
    await getJson<{ execution_status: string }>(
      request,
      `${API}/conversations/${cid}/state`,
    )
  ).execution_status;
}

async function events(request: APIRequestContext, cid: string): Promise<WireEvent[]> {
  const body = await getJson<{ events?: WireEvent[] } | WireEvent[]>(
    request,
    `${API}/conversations/${cid}/events`,
  );
  return Array.isArray(body) ? body : (body.events ?? []); // tolerate either wire shape
}

test("stop → resume continues the same run to FINISHED (live)", async ({ page, request }) => {
  test.setTimeout(1_200_000); // live build on the local 27B + a stop/resume cycle

  // 1. Backend health — skip, never crash, when the agent-server is down.
  let healthy = false;
  try {
    healthy = (await request.get(`${API}/health`, { timeout: 5_000 })).ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start it and re-run`);

  // 2. Fresh conversation; /build/{cid} is the RESUME path (chat-pane composer).
  const conv = await (
    await request.post(`${API}/conversations`, {
      data: { surface: "build", title: "BP-12 UI rung: stop → resume" },
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

  // 4. Let it do REAL work first: wait until at least 2 observations landed, so
  //    the stop is mid-run (a stop before any work would make "resume continues"
  //    indistinguishable from "started fresh").
  const workDeadline = Date.now() + 300_000;
  let observed = 0;
  while (Date.now() < workDeadline) {
    observed = (await events(request, cid)).filter((e) => e.kind === "observation").length;
    if (observed >= 2) break;
    await new Promise((r) => setTimeout(r, 3_000));
  }
  expect(observed, "agent never produced 2 observations to stop between").toBeGreaterThanOrEqual(2);

  // Pre-stop fingerprint: one approved plan, N actions.
  const preEvents = await events(request, cid);
  const prePlans = preEvents.filter((e) => e.kind === "plan").length;
  const preActions = preEvents.filter((e) => e.kind === "action").length;

  // 5. Stop (graceful, cooperative — honored at the next loop checkpoint).
  //    AgentLoop.cancel() emits IDLE detail="cancelled" (engine.py) — the live
  //    build stop lands IDLE, not PAUSED (PAUSED is the crash-reconciler / DR
  //    path). Resume legality covers both; the spec accepts both.
  await page
    .getByRole("button", { name: /stop the agent gracefully/i })
    .click({ timeout: 30_000 });
  const stopDeadline = Date.now() + 300_000;
  let st = "?";
  while (Date.now() < stopDeadline) {
    st = await state(request, cid);
    if (st === "PAUSED" || st === "IDLE") break;
    // A fast model can outrun the stop — FINISHED before the cancel checkpoint.
    if (st === "FINISHED") break;
    await new Promise((r) => setTimeout(r, 3_000));
  }
  test.skip(st === "FINISHED", "build finished before the cooperative stop landed — rerun");
  expect(["PAUSED", "IDLE"], `stop never reached a stopped state (got ${st})`).toContain(st);

  // 6. Stopped UI truth: status pill (Paused/Stopped per STATUS_LABEL) + Resume button.
  const stoppedPill = st === "PAUSED" ? "Paused" : "Stopped";
  await expect(page.getByText(stoppedPill, { exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });
  const resumeBtn = page.getByRole("button", {
    name: /resume the agent — continue this build/i,
  });
  await expect(resumeBtn, "Resume button missing while PAUSED").toBeVisible({ timeout: 15_000 });
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "resume-paused.png") });

  // 7. Resume → RUNNING again (continuation, not a fresh run).
  await resumeBtn.click();
  const resumeDeadline = Date.now() + 120_000;
  while (Date.now() < resumeDeadline) {
    st = await state(request, cid);
    if (st === "RUNNING" || st === "FINISHED") break;
    await new Promise((r) => setTimeout(r, 2_000));
  }
  expect(["RUNNING", "FINISHED"], `resume left status ${st}`).toContain(st);
  await expect(
    page.getByText("Working", { exact: true }).first(),
    "status pill never returned to Working after Resume",
  ).toBeVisible({ timeout: 60_000 });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "resume-running.png") });

  // 8. FINISHED, and the run CONTINUED: exactly the same plan (no re-plan), more
  //    actions than before the stop.
  const finishDeadline = Date.now() + 600_000;
  while (Date.now() < finishDeadline) {
    st = await state(request, cid);
    if (["FINISHED", "ERROR"].includes(st)) break;
    await new Promise((r) => setTimeout(r, 5_000));
  }
  expect(st, `run ended ${st}`).toBe("FINISHED");
  await expect(page.getByText("Finished", { exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });

  const postEvents = await events(request, cid);
  const postPlans = postEvents.filter((e) => e.kind === "plan").length;
  const postActions = postEvents.filter((e) => e.kind === "action").length;
  expect(postPlans, "resume re-planned instead of continuing").toBe(prePlans);
  expect(postActions, "no further actions after resume").toBeGreaterThan(preActions);
  const resumedNotes = postEvents.filter(
    (e) =>
      e.kind === "message" &&
      e.source === "environment" &&
      (e.message?.content ?? "") === "Resumed by user.",
  ).length;
  expect(resumedNotes, "resume env message not exactly-once").toBe(1);

  fs.mkdirSync(RECORD_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(RECORD_DIR, "resume-ui-witness.json"),
    JSON.stringify(
      { cid, prePlans, preActions, postPlans, postActions, resumedNotes, status: st },
      null,
      2,
    ),
  );
});
