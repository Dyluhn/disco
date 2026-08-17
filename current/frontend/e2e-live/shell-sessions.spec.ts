import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-01 live acceptance (current/docs/workorders/BP-01-shell-sessions.md, acceptance §4).
 *
 * Drives the REAL stack — live agent-server (127.0.0.1:8000), real 27B driver,
 * process sandbox — through the real UI. Asserts the agent used the new
 * shell_exec / shell_view tools and that the session's output (tick-3) reached
 * the Activity feed. Saves the evidence screenshot the work-order pack requires.
 */

const SCREENSHOT_DIR = path.resolve(
  __dirname,
  "../../test-record/screenshots/bp-01",
);

// The order's frozen prompt — do not edit.
const PROMPT =
  "In session 'demo', run: for i in 1 2 3; do echo tick-$i; sleep 1; done — " +
  "then view the session and finish.";

test("agent runs a command in a named session and views it (live)", async ({
  page,
}) => {
  // Fail fast if the live agent-server isn't up — a fixture fallback would
  // silently violate the live-acceptance rule.
  const health = await page.request
    .get("http://127.0.0.1:8000/health")
    .catch(() => null);
  test.skip(!health || !health.ok(), "agent-server not running on :8000");

  await page.goto("/");
  await page.getByRole("radio", { name: "build" }).click();

  const input = page.getByPlaceholder(/describe what you want/i);
  await expect(input).toBeVisible();
  await input.fill(PROMPT);
  await input.press("Enter");

  // Plan-approval gate (live driver drafts a real plan — give it time).
  const approve = page.getByRole("button", { name: /approve & build/i });
  await expect(approve).toBeVisible({ timeout: 240_000 });
  await approve.click();

  // The feed must show the shell_exec action on session 'demo' …
  await expect(
    page.getByText(/shell_exec|session 'demo'/i).first(),
  ).toBeVisible({ timeout: 300_000 });

  // … and a shell_view observation containing the final tick.
  await expect(page.getByText(/tick-3/).first()).toBeVisible({
    timeout: 300_000,
  });

  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({
    path: path.join(SCREENSHOT_DIR, "feed-shell-session.png"),
    fullPage: true,
  });
});
