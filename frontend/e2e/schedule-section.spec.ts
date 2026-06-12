import { expect, test } from "@playwright/test";

/**
 * RP-08 — the schedule UI is REACHABLE and renders the confirm card with the
 * next-3 run times. ScheduleSection mounts in the Build surface once a build is
 * settled (it needs a persisted conversation id). The schedule API is stubbed at
 * the HTTP boundary (fixture mode has no backend) — the component, the client NL
 * parse, the preview flow, and the confirm-card render all execute for real.
 */
test.describe("Build → schedule a re-run", () => {
  test("the schedule section is reachable and shows the next-3-runs confirm card", async ({
    page,
  }) => {
    // Stub the schedule endpoints (relative, same-origin in fixture mode).
    await page.route("**/api/conversations/**/schedules", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({ json: { schedules: [] } });
      } else {
        await route.fulfill({ json: { schedule_id: "sched_demo", enabled: true } });
      }
    });
    await page.route("**/api/schedules/preview", async (route) => {
      await route.fulfill({
        json: {
          rrule: "0 9 * * *",
          next_runs: [
            "2026-06-12T09:00:00Z",
            "2026-06-13T09:00:00Z",
            "2026-06-14T09:00:00Z",
          ],
        },
      });
    });

    // Drive the build fixture to FINISHED (reuses the proven build.spec flow).
    await page.goto("/");
    await page.getByRole("radio", { name: "build" }).click();
    const input = page.getByPlaceholder(/describe what you want/i);
    await input.fill("Write a fizzbuzz script, run it, then delete it to clean up.");
    await input.press("Enter");
    await page.getByRole("button", { name: /approve & build/i }).click();
    await page.getByRole("button", { name: /approve & run/i }).click();
    await expect(page.getByText(/wrote fizzbuzz\.py/i)).toBeVisible();

    // The settled build now exposes the schedule section (it has a cid).
    const section = page.locator('section[aria-labelledby="schedule-heading"]');
    await expect(section.getByRole("heading", { name: /schedules/i })).toBeVisible();

    // Open the create form, enter a cron, preview → confirm card with next 3 runs.
    await section.getByRole("button", { name: /new schedule/i }).click();
    await section.getByPlaceholder(/every day at 9am/i).fill("0 9 * * *");
    await section.getByRole("button", { name: /preview schedule/i }).click();

    await expect(section.getByText(/next 3 runs/i)).toBeVisible();
    await expect(section.getByRole("button", { name: /save schedule/i })).toBeVisible();

    await section.screenshot({ path: "e2e/_artifacts/rp-08-schedule-card.png" });
  });
});
