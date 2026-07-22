import { expect, test } from "@playwright/test";

/**
 * BP-02 live acceptance (docs/workorders/BP-02, acceptance §4) — preview TRUTH.
 *
 * Real stack end-to-end: live agent-server on :8000 with the gVisor backend
 * (VM-201 over SSH), real 27B driver, real UI. Proves:
 *   1. a live build writes index.html → the auto-served 'preview' session serves
 *      it through the proxy iframe, and the caption names the true owner;
 *   2. the agent kills the preview session → the UI reports the preview down —
 *      truth, not a stale "serving" (the old supervised keepalive would lie here);
 *   3. the Refresh button (ensure_preview path) brings it back, caption returns.
 * Screenshots: preview-owner.png / preview-down.png / preview-restarted.png.
 */

const AGENT_API = (
  process.env.DISCO_RELIABILITY_AGENT_URL ?? "http://127.0.0.1:8000"
).replace(/\/$/, "");

const BUILD_PROMPT =
  "Create a static web page: write an index.html in the workspace root with the " +
  'heading "BP02 preview truth". Then finish.';

const KILL_PROMPT =
  "Kill the shell session named 'preview' (shell_kill_process). Confirm with " +
  "server_status that port 8000 is FREE, then finish.";

async function approvePlan(page: import("@playwright/test").Page) {
  const approve = page.getByRole("button", { name: /approve & build/i });
  await expect(approve).toBeVisible({ timeout: 240_000 });
  await approve.click();
}

test("preview pane tells the truth about who serves :8000 (live)", async ({
  page,
}, testInfo) => {
  const health = await page.request
    .get(`${AGENT_API}/health`)
    .catch(() => null);
  test.skip(!health || !health.ok(), "agent-server not running on :8000");

  // The page stays on "/" for an inline build — capture the conversation id from
  // the UI's own API traffic so part 3 can poll the preview endpoint directly.
  let cid = "";
  page.on("request", (req) => {
    const m = req.url().match(/\/conversations\/(conv_[a-f0-9]+)\//);
    if (m && !cid) cid = m[1];
  });

  await page.goto("/");
  // A durable stack may legitimately resume its latest unfinished build on the
  // index route. Exercise the product's New affordance so this journey always
  // starts a distinct conversation without requiring an empty database.
  await page.getByRole("link", { name: "New" }).click();
  await page.getByRole("radio", { name: "build" }).click();

  const input = page.getByPlaceholder(/describe what you want/i);
  await expect(input).toBeVisible();
  await input.fill(BUILD_PROMPT);
  await input.press("Enter");
  await approvePlan(page);

  // ---- 1. live serving truth: proxy up, owner caption names http.server ----
  await page.getByRole("tab", { name: /preview/i }).click();
  const liveToggle = page.getByRole("button", { name: /live server/i });
  await expect(liveToggle).toBeVisible({ timeout: 300_000 });
  await liveToggle.click();

  const caption = page.getByText(/Serving: .*http\.server/);
  await expect(caption).toBeVisible({ timeout: 60_000 });
  await expect(
    page
      .frameLocator('iframe[title="Live preview"]')
      .getByText("BP02 preview truth"),
  ).toBeVisible({ timeout: 60_000 });
  await page.screenshot({
    path: testInfo.outputPath("preview-owner.png"),
    fullPage: true,
  });

  // ---- 2. the agent kills its preview session → the UI reports DOWN ----
  // After FINISHED the bottom input is a fresh "Plan a change to this build…"
  // box (BuildSurface re-enter-plan-mode), not the original prompt input.
  const replan = page.getByPlaceholder(/plan a change to this build/i);
  await expect(replan).toBeVisible({ timeout: 300_000 });
  await replan.fill(KILL_PROMPT);
  await replan.press("Enter");
  await approvePlan(page);

  // availability polls every 4s; the live pane falls back once the proxy is down.
  await expect(caption).toBeHidden({ timeout: 300_000 });
  await page.screenshot({
    path: testInfo.outputPath("preview-down.png"),
    fullPage: true,
  });

  // ---- 3. one-click recovery AFTER the kill build finishes ----
  // Clicking Refresh while the agent is still mid-plan races it: its next step is
  // "confirm 8000 is FREE", so a premature revive gets killed right back (run-3
  // failure mode). Wait for the run to actually FINISH first. (At this point the
  // status is RUNNING — the caption just disappeared because the agent killed the
  // server — so polling for FINISHED can't match stale state from build 1.)
  // Note: without a configured projects_root there is no snapshot/teardown on
  // finish, so the box stays alive and Refresh exercises the live-session
  // ensure_preview path; the torn-down re-materialize path is pinned by unit
  // tests in test_sandbox_config.py.
  expect(cid, "conversation id captured from API traffic").toBeTruthy();
  await expect
    .poll(
      async () => {
        const r = await page.request.get(
          `${AGENT_API}/conversations/${cid}/state`,
        );
        return ((await r.json()) as { execution_status?: string })
          .execution_status;
      },
      { timeout: 300_000, intervals: [2_000] },
    )
    .toBe("FINISHED");

  await page.getByRole("button", { name: /refresh|restarting/i }).click();
  // The pane's view mode is sticky: part 1 toggled "live", so the moment the
  // proxy comes back (4s availability poll) it returns STRAIGHT to the live
  // view with the owner caption — there is no "Live server" button to click
  // again (that toggle only renders in the rendered view).
  await expect(page.getByText(/Serving: .*http\.server/)).toBeVisible({
    timeout: 120_000,
  });
  // the revived preview serves the REHYDRATED snapshot, not an empty workspace
  await expect(
    page
      .frameLocator('iframe[title="Live preview"]')
      .getByText("BP02 preview truth"),
  ).toBeVisible({ timeout: 60_000 });
  await page.screenshot({
    path: testInfo.outputPath("preview-restarted.png"),
    fullPage: true,
  });
});
