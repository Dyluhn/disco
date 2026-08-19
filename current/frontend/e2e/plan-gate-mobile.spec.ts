import { type Page, expect, test } from "@playwright/test";

/**
 * Regression for the mobile plan-gate header overlap: at narrow widths, the
 * "NEEDS YOUR APPROVAL" pill (and the revision chip) used to render on TOP of
 * the "Review the plan" heading — illegible, and on the card every Build /
 * Agent / Deep Research run gates on. `PlanPanelHeader` now wraps instead of
 * overflowing (see PlanPanel.tsx). This is a REAL layout assertion (actual
 * Firefox bounding boxes), not a jsdom/vitest one — jsdom has no layout engine
 * (getBoundingClientRect is always zero there), so an overlap check in the
 * unit-test file would be meaningless.
 *
 * Also asserts the approve/reject actions meet the 44px CSS px minimum touch
 * target at these widths (mobile spec requirement).
 */

const MOBILE_VIEWPORTS = [
  { name: "375x667", width: 375, height: 667 },
  { name: "430x932", width: 430, height: 932 },
];

function rectsOverlap(
  a: { x: number; y: number; width: number; height: number },
  b: { x: number; y: number; width: number; height: number },
): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}

async function reachBuildPlanGate(page: Page) {
  await page.goto("/");
  await page.getByRole("radio", { name: "build" }).click();
  const input = page.getByPlaceholder(/describe what you want/i);
  await input.fill("Write a fizzbuzz script, run it, then delete it to clean up.");
  await input.press("Enter");
}

for (const vp of MOBILE_VIEWPORTS) {
  test.describe(`plan-gate header, mobile @ ${vp.name}`, () => {
    test.use({ viewport: { width: vp.width, height: vp.height } });

    test(`the "needs your approval" pill and revision chip never overlap the heading @ ${vp.name}`, async ({
      page,
    }) => {
      await reachBuildPlanGate(page);

      const heading = page.getByText("Review the plan", { exact: true });
      const pill = page.getByText(/needs your approval/i);
      const revisionChip = page.getByText(/^revision \d+$/i);
      await expect(heading).toBeVisible();
      await expect(pill).toBeVisible();
      await expect(revisionChip).toBeVisible();

      const headingBox = await heading.boundingBox();
      const pillBox = await pill.boundingBox();
      const revisionBox = await revisionChip.boundingBox();
      expect(headingBox).not.toBeNull();
      expect(pillBox).not.toBeNull();
      expect(revisionBox).not.toBeNull();
      if (!headingBox || !pillBox || !revisionBox) return;

      expect(rectsOverlap(headingBox, pillBox)).toBe(false);
      expect(rectsOverlap(headingBox, revisionBox)).toBe(false);

      // The heading must also fully fit within the viewport width — no
      // silent horizontal overflow/scroll as a side effect of the fix.
      expect(headingBox.x).toBeGreaterThanOrEqual(0);
      expect(headingBox.x + headingBox.width).toBeLessThanOrEqual(vp.width);

      await page.screenshot({ path: `e2e/_artifacts/plan-gate-mobile-${vp.name}.png` });
    });

    test(`Approve & build / Revise… meet the 44px touch-target minimum @ ${vp.name}`, async ({
      page,
    }) => {
      await reachBuildPlanGate(page);

      const approve = page.getByRole("button", { name: /approve & build/i });
      const revise = page.getByRole("button", { name: "Revise…" });
      await expect(approve).toBeVisible();
      await expect(revise).toBeVisible();

      const approveBox = await approve.boundingBox();
      const reviseBox = await revise.boundingBox();
      expect(approveBox).not.toBeNull();
      expect(reviseBox).not.toBeNull();
      if (!approveBox || !reviseBox) return;

      expect(approveBox.height).toBeGreaterThanOrEqual(44);
      expect(reviseBox.height).toBeGreaterThanOrEqual(44);
      // Stacked, full-width, primary (Approve) on top — not two adjacent
      // small targets a thumb can miss.
      expect(approveBox.width).toBeGreaterThan(200);
      expect(reviseBox.width).toBeGreaterThan(200);
      expect(approveBox.y).toBeLessThan(reviseBox.y);
    });
  });
}
