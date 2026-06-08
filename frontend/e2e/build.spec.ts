import { expect, test } from "@playwright/test";

test.describe("Build → plan → stream → confirm", () => {
  test("plan gate, risky-action confirmation, and agent activity", async ({
    page,
  }) => {
    await page.goto("/");

    // Switch from Search to Build.
    await page.getByRole("radio", { name: "build" }).click();

    const input = page.getByPlaceholder(/describe what you want/i);
    await expect(input).toBeVisible();
    await input.fill(
      "Write a fizzbuzz script, run it, then delete it to clean up.",
    );
    await input.press("Enter");

    // Plan-approval gate (honest blocking state, no auto-run).
    const approve = page.getByRole("button", { name: /approve & build/i });
    await expect(approve).toBeVisible();
    await expect(page.getByText(/fizzbuzz/i).first()).toBeVisible();
    await approve.click();

    // The risky `rm` step hits the confirmation gate before executing.
    const confirmRun = page.getByRole("button", { name: /approve & run/i });
    await expect(confirmRun).toBeVisible();
    await confirmRun.click();

    // The activity feed renders the agent's work in plain language.
    await expect(page.getByText(/wrote fizzbuzz\.py/i)).toBeVisible();
  });

  test("a running build exposes a Kill control", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "build" }).click();
    const input = page.getByPlaceholder(/describe what you want/i);
    await input.fill("Write a fizzbuzz script, run it, then delete it to clean up.");
    await input.press("Enter");

    // Before/at the plan gate the agent is live; the Kill switch is reachable.
    await expect(
      page.getByRole("button", { name: /kill/i }),
    ).toBeVisible();
  });
});
