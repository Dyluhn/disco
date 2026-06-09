import { expect, test } from "@playwright/test";

// Visual + functional proof of the build-control safety polish: Kill is gated
// behind an inline confirm so a mis-click can't tear down a long build.
test.describe("Build controls — Kill confirmation", () => {
  test("Kill arms an inline confirm before the destructive teardown", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "build" }).click();
    const input = page.getByPlaceholder(/describe what you want/i);
    await input.fill("Write a fizzbuzz script, run it, then delete it to clean up.");
    await input.press("Enter");

    // Reach a live state where Kill is meaningful (the plan-approval gate).
    const kill = page.getByRole("button", { name: /kill the agent/i });
    await expect(kill).toBeVisible();

    // First click ARMS the confirm — it must NOT immediately kill.
    await kill.click();
    await expect(page.getByText(/kill this run\?/i)).toBeVisible();
    const confirm = page.getByRole("button", { name: /confirm kill/i });
    await expect(confirm).toBeVisible();

    // SCREENSHOT: the armed confirm (a mis-click can't nuke the build).
    await page.screenshot({ path: "e2e/_artifacts/kill-confirm.png", fullPage: true });

    // Cancel keeps the run; the armed Kill button returns.
    await page.getByRole("button", { name: /keep the run/i }).click();
    await expect(page.getByText(/kill this run\?/i)).toBeHidden();
    await expect(kill).toBeVisible();
  });
});

test.describe("Build surface — sticky plan tracker", () => {
  test("the plan stays pinned while the activity feed scrolls beneath it", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "build" }).click();
    const input = page.getByPlaceholder(/describe what you want/i);
    await input.fill("Write a fizzbuzz script, run it, then delete it to clean up.");
    await input.press("Enter");

    // Approve the plan + the risky action so the run finishes with a full feed.
    await page.getByRole("button", { name: /approve & build/i }).click();
    await page.getByRole("button", { name: /approve & run/i }).click();
    await expect(page.getByText(/fizzbuzz ran correctly/i)).toBeVisible();

    // The read-only plan tracker (a plan step capstone) is on screen…
    const planStep = page.getByText(/Write fizzbuzz\.py/i).first();
    await expect(planStep).toBeVisible();
    const before = await planStep.boundingBox();

    // …scroll the feed to the bottom; a sticky plan stays put (pinned to the top).
    await page.mouse.move(440, 400);
    await page.mouse.wheel(0, 4000);
    await page.waitForTimeout(300);
    await expect(planStep).toBeVisible(); // still in the viewport after scrolling
    const after = planStep ? await planStep.boundingBox() : null;
    // its top barely moves (pinned), unlike feed content which scrolls away.
    if (before && after) expect(Math.abs(after.y - before.y)).toBeLessThan(40);

    await page.screenshot({ path: "e2e/_artifacts/sticky-plan.png", fullPage: false });
  });
});
