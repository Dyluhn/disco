/**
 * WO-C3 §7.8 / §7.12 — frozen Firefox proof: the `needs_review` SelfHostPanel state,
 * in every UI mounting mode (Build surface + Agent surface).
 *
 * FROZEN acceptance path (plan §1.1). Firefox-only. Locked semantics §2.6 + plan §7
 * acceptance 8: for `needs_review` every blocker is rendered, no run command appears,
 * the bound self-host download is unavailable, and the plain source download remains.
 *
 * The needs_review verdict is the offline `conv_demo_landing` fixture (self_host:false,
 * blocker `release_field_unresolved`).
 *
 * REACHABILITY PRECONDITION (deferred to orchestrator frontend-lane wiring):
 * As with the candidate proof, the SelfHostPanel mounts only for a FINISHED build
 * whose cid is a demo release cid. Opening `/build/conv_demo_landing` resumes WITHOUT
 * a kick (stays IDLE), so the panel does not mount offline today. This spec is authored
 * to the intended contract and validates as soon as a fixture-mode finished demo makes
 * the panel reachable. On baseline it is RED (panel unreachable). NOTE: the panel's
 * needs_review branch is already honest, so once reachable this becomes a
 * GREEN-PRESERVATION proof. Live validation is deferred.
 */

import { expect, test, type Page } from "@playwright/test";

/** Assert the exact `needs_review` blocker/action set and screenshot the blockers
 * visible WITHOUT interaction. */
async function assertNeedsReviewPanel(page: Page, artifact: string): Promise<void> {
  const panel = page.locator('[data-disco-control="build.self-host"]');
  await expect(panel).toBeVisible();
  await expect(panel.locator('[data-self-host-status="needs_review"]')).toBeVisible();

  // The blocker is rendered (structured code + human message) without interaction.
  await expect(panel.locator('[data-blocker-code="release_field_unresolved"]')).toBeVisible();
  await expect(panel.getByText(/no single web entrypoint could be resolved/i)).toBeVisible();

  // No run command; no self-host-ready wording.
  await expect(panel.locator('[data-disco-control="build.self-host-command"]')).toHaveCount(0);
  await expect(panel.getByText(/ready to self-host/i)).toHaveCount(0);

  // The plain source download remains available.
  await expect(panel.locator('[data-disco-control="build.download-source"]')).toBeVisible();

  await page.screenshot({ path: artifact, fullPage: true });
}

test.describe("WO-C3 §7.12 — SelfHostPanel needs_review (frozen Firefox proof)", () => {
  test("Build surface: needs_review shows the blocker and no run command", async ({ page }) => {
    await page.goto("/build/conv_demo_landing");
    await assertNeedsReviewPanel(page, "e2e/_artifacts/closeout-selfhost-needs-review-build.png");
  });

  test("Agent surface: the same needs_review verdict renders the identical blocker set", async ({
    page,
  }) => {
    await page.goto("/agent/conv_demo_landing");
    await assertNeedsReviewPanel(page, "e2e/_artifacts/closeout-selfhost-needs-review-agent.png");
  });
});
