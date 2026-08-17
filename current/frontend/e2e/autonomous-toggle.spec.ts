import { expect, test } from "@playwright/test";

// Visual + functional proof of the autonomous-mode control (issue A): a toggle in
// the build start composer that the user flips BEFORE running a build. Off by
// default (interactive — the agent can pause and ask). Rule 7: video on.
test.use({ video: "on" });

test.describe("Autonomous mode — composer toggle", () => {
  test("toggle is off by default and flips on with an accessible switch", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "build" }).click();

    const toggle = page.getByRole("switch", { name: /autonomous/i });
    await expect(toggle).toBeVisible();

    // Default: OFF (interactive — the safe default for hard tasks).
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await expect(toggle).toHaveText(/autonomous: off/i);
    await page.screenshot({ path: "e2e/_artifacts/autonomous-off.png", fullPage: true });

    // Flip ON.
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect(toggle).toHaveText(/autonomous: on/i);
    await page.screenshot({ path: "e2e/_artifacts/autonomous-on.png", fullPage: true });

    // Flip back OFF — it's a real two-way control.
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "false");
  });
});
