import { expect, test } from "@playwright/test";

// Visual + functional proof of the autonomous-mode control (issue A): a toggle in
// the build composer's options menu that the user flips BEFORE running a build. ON
// by default (hands-off runs are the primary flow); flipping it off opts into the
// interactive mode where the agent can pause and ask. Rule 7: video on.
test.use({ video: "on" });

test.describe("Autonomous mode — composer toggle", () => {
  test("toggle is on by default and flips with an accessible switch", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "build" }).click();

    // The toggle lives in the composer's collapsible options menu now.
    await page.locator('[data-disco-control="build.options"]').click();
    const toggle = page.getByRole("switch", { name: /autonomous/i });
    await expect(toggle).toBeVisible();

    // Default: ON (headless — the hands-off primary flow).
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await page.screenshot({ path: "e2e/_artifacts/autonomous-on.png", fullPage: true });

    // Flip OFF (interactive — the agent can pause and ask).
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await page.screenshot({ path: "e2e/_artifacts/autonomous-off.png", fullPage: true });

    // Flip back ON — it's a real two-way control.
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "true");
  });
});
