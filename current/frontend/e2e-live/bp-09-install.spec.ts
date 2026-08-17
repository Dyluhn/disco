import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-09 UI rung (live, DRIVES A BUILD): the behavioral scenario through the real
 * UI — a build that genuinely needs a dependency (express), open egress. Assert
 * the feed surfaces the install running in a session (BP-14's Terminal tab has
 * not landed yet, so per the order the feed observation is the accepted surface)
 * and the Preview renders the served JSON.
 *
 * Evidence: test-record/screenshots/bp-09/install-and-serve.png
 *           test-record/bp-09/install-ui-witness.json
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-09");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-09");

const PROMPT =
  "Build a small Express server that listens on port 8000 and returns JSON " +
  '{"service": "bp09-status", "ok": true} at the root path. Install express ' +
  "first — it is not preinstalled. Make sure the server actually responds, " +
  "then finish.";

test("install runs in a session and the served JSON reaches the Preview (live)", async ({
  page,
  request,
}) => {
  test.setTimeout(900_000); // live build + npm install on the local 27B takes minutes

  // 1. Backend health — skip, never crash, when the agent-server is down.
  let healthy = false;
  try {
    healthy = (await request.get(`${API}/health`, { timeout: 5_000 })).ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start it and re-run`);

  // 2. Fresh conversation, prompt, approve.
  const conv = await (
    await request.post(`${API}/conversations`, {
      data: { surface: "build", title: "BP-09 UI rung: install express + serve JSON" },
    })
  ).json();
  const cid: string = conv.conversation_id;

  await page.setViewportSize({ width: 1280, height: 2200 }); // bp-04 lesson: no autoscroll under shots
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  // /build/{cid} is the RESUME path (useBuild.ts:23-26 → started=true), so the
  // surface renders the chat-pane composer ("Plan a change to this build…" →
  // requestPlan WS frame), NOT the front-door one (which creates its OWN cid).
  const composer = page.getByPlaceholder(/plan a change to this build/i);
  await composer.fill(PROMPT, { timeout: 30_000 });
  await composer.press("Enter");

  const approve = page.getByRole("button", { name: /approve\s*&\s*build/i });
  await approve.click({ timeout: 300_000 });

  // 3. Wait for FINISH (poll state via API — steadier than UI text alone).
  const deadline = Date.now() + 600_000;
  let status = "?";
  while (Date.now() < deadline) {
    status = (await (await request.get(`${API}/conversations/${cid}/state`)).json())
      .execution_status;
    if (["FINISHED", "ERROR"].includes(status)) break;
    await new Promise((r) => setTimeout(r, 5_000));
  }
  expect(status, `run ended ${status}`).toBe("FINISHED");
  await expect(page.getByText("Finished", { exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });

  // 4. The feed shows the install running in a session (command row mentions
  //    npm/pnpm + express). buildTrace renders shell commands as "Ran …" rows.
  const installRow = page.getByText(/(npm|pnpm)\b.*(install|add|i)\b.*express/i).first();
  await expect(installRow, "no feed row shows the express install").toBeVisible({
    timeout: 30_000,
  });

  // 5. Preview renders the served JSON (live-server mode through the proxy).
  await page.getByRole("tab", { name: /preview/i }).first().click();
  const frame = page.frameLocator("iframe").first();
  await expect(frame.getByText(/bp09-status/).first(), "served JSON missing").toBeVisible({
    timeout: 60_000,
  });

  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "install-and-serve.png") });

  fs.mkdirSync(RECORD_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(RECORD_DIR, "install-ui-witness.json"),
    JSON.stringify({ cid, status }, null, 2),
  );
});
