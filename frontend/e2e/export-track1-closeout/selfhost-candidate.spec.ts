/**
 * WO-C3 §7.7 / §7.12 — frozen Firefox proof: the `candidate` SelfHostPanel state, in
 * every UI mounting mode (Build surface + Agent surface).
 *
 * FROZEN acceptance path (plan §1.1). Firefox-only. Locked semantics §2.1: a
 * `candidate` is statically plausible and UNVERIFIED — the panel must read exactly
 * "Bundle available" + "Not runtime-verified", must never say "Ready", and MAY show
 * the exact local run command (an internally complete bundle). The plain source
 * download stays available.
 *
 * The candidate verdict is the offline `conv_demo_snake` fixture (self_host:true).
 *
 * REACHABILITY PRECONDITION (deferred to orchestrator frontend-lane wiring):
 * In the current offline app the SelfHostPanel mounts only for a conversation that is
 * BOTH at `status === "FINISHED"` AND whose cid is a demo release cid. A fresh build
 * uses `conv_fixture` (→ not_web); opening `/build/conv_demo_snake` resumes WITHOUT a
 * kick, so it stays IDLE and the panel never mounts. There is therefore no offline
 * path today that surfaces the candidate verdict THROUGH SelfHostPanel (it surfaces
 * only as the ProjectsView row badge — a separate control). This spec is authored to
 * the intended contract and asserts it as soon as a fixture-mode finished demo (or
 * the C3 impl surfacing the panel for a resumed finished project) makes the panel
 * reachable. On baseline (581d1fbe) it is RED SOLELY because the panel is unreachable
 * offline. Once reachable this is a green-preservation proof: the candidate StatusPill
 * already renders "Bundle available" + "Not runtime-verified" (never "Ready") and the
 * self-hostable branch already shows the run command — the WO-C3/C5 honest copy is in
 * place, so the copy/run-command are NOT the defect. Its live validation is deferred.
 */

import { expect, test, type Page } from "@playwright/test";

/** Assert the exact `candidate` copy/action set on the SelfHostPanel and screenshot
 * the uncertainty ("Not runtime-verified") visible WITHOUT interaction. */
async function assertCandidatePanel(page: Page, artifact: string): Promise<void> {
  const panel = page.locator('[data-disco-control="build.self-host"]');
  await expect(panel).toBeVisible();

  // Exact honest, unverified copy (§2.1) — visible without interaction.
  await expect(panel.getByText("Bundle available")).toBeVisible();
  await expect(panel.getByText(/not runtime-verified/i)).toBeVisible();

  // Never "Ready"/verified-as-ready anywhere in the panel DOM.
  const text = (await panel.textContent()) ?? "";
  expect(text.toLowerCase()).not.toContain("ready");

  // A candidate MAY show the exact local run command (an internally complete
  // bundle); never claims "Ready".
  await expect(panel.locator('[data-disco-control="build.self-host-command"]')).toBeVisible();

  // The plain source download remains available.
  await expect(panel.locator('[data-disco-control="build.download-source"]')).toBeVisible();

  await page.screenshot({ path: artifact, fullPage: true });
}

test.describe("WO-C3 §7.12 — SelfHostPanel candidate (frozen Firefox proof)", () => {
  test("Build surface: a candidate reads 'Bundle available' / 'Not runtime-verified', never 'Ready'", async ({
    page,
  }) => {
    await page.goto("/build/conv_demo_snake");
    await assertCandidatePanel(page, "e2e/_artifacts/closeout-selfhost-candidate-build.png");
  });

  test("Agent surface: the same candidate verdict renders the identical honest copy", async ({
    page,
  }) => {
    await page.goto("/agent/conv_demo_snake");
    await assertCandidatePanel(page, "e2e/_artifacts/closeout-selfhost-candidate-agent.png");
  });
});
