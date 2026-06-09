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
