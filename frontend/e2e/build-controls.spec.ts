import { expect, test } from "@playwright/test";

async function seedOverflowingActivityFeed(page: import("@playwright/test").Page) {
  const feed = page.getByTestId("build-activity-feed");
  await feed.evaluate((node) => {
    // Keep the overflow on the React-owned viewport itself. Injecting an
    // unowned child is racy: the state update that reveals the dock also asks
    // React to reconcile the feed and legitimately removes that child.
    node.style.height = "320px";
    node.style.flex = "0 0 320px";
    node.style.paddingBottom = "4000px";
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  await expect(page.getByRole("button", { name: "Scroll to latest activity" })).toBeVisible();
}

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

test.describe("Build surface — scroll-to-latest accessibility", () => {
  test("the chevron has a non-overlapping dock across width, zoom, and streaming changes", async ({
    page,
  }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "build" }).click();
    const input = page.getByPlaceholder(/describe what you want/i);
    await input.fill("Write a fizzbuzz script, run it, then delete it to clean up.");
    await input.press("Enter");
    await expect(page.getByTestId("build-activity-feed")).toBeVisible();
    await seedOverflowingActivityFeed(page);

    const assertDocked = async () => {
      const feedBox = await page.getByTestId("build-activity-feed").boundingBox();
      const dockBox = await page.getByTestId("build-scroll-to-latest-dock").boundingBox();
      const buttonBox = await page
        .getByRole("button", { name: "Scroll to latest activity" })
        .boundingBox();
      expect(feedBox).not.toBeNull();
      expect(dockBox).not.toBeNull();
      expect(buttonBox).not.toBeNull();
      if (!feedBox || !dockBox || !buttonBox) return;
      expect(dockBox.y).toBeGreaterThanOrEqual(feedBox.y + feedBox.height - 1);
      expect(buttonBox.y).toBeGreaterThanOrEqual(feedBox.y + feedBox.height - 1);
      expect(buttonBox.x).toBeGreaterThanOrEqual(0);
      expect(buttonBox.x + buttonBox.width).toBeLessThanOrEqual(
        await page.evaluate(() => window.innerWidth),
      );
    };

    await test.step("desktop at 100%", assertDocked);

    const button = page.getByRole("button", { name: "Scroll to latest activity" });
    for (
      let attempts = 0;
      attempts < 40 && !(await button.evaluate((el) => el === document.activeElement));
      attempts += 1
    ) {
      await page.keyboard.press("Tab");
    }
    await expect(button).toBeFocused();
    const focusStyle = await button.evaluate((el) => {
      const style = getComputedStyle(el);
      return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth };
    });
    expect(focusStyle.outlineStyle).not.toBe("none");
    expect(Number.parseFloat(focusStyle.outlineWidth)).toBeGreaterThan(0);

    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.getByTestId("build-activity-feed").evaluate((node) => {
      node.scrollTo = ((options: ScrollToOptions) => {
        node.dataset.f15ScrollBehavior = options.behavior ?? "auto";
      }) as typeof node.scrollTo;
    });
    await button.press("Enter");
    await expect(page.getByTestId("build-activity-feed")).toHaveAttribute(
      "data-f15-scroll-behavior",
      "auto",
    );

    // The activation above intentionally dismisses the dock. Recreate the
    // overflow before checking the same layout contract at other viewports.
    await seedOverflowingActivityFeed(page);

    await test.step("desktop at 200% zoom", async () => {
      await page.evaluate(() => {
        document.documentElement.style.zoom = "2";
      });
      await assertDocked();
    });

    await test.step("mobile width with a growing stream", async () => {
      await page.evaluate(() => {
        document.documentElement.style.zoom = "1";
      });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.getByTestId("build-activity-feed").evaluate((node) => {
        // The narrow layout can grow with the page instead of overflowing its
        // feed. Constrain the viewport to exercise the actual chevron state.
        node.style.height = "240px";
        node.style.flex = "0 0 240px";
        node.style.paddingBottom = "5600px";
        node.scrollTop = 0;
        node.dispatchEvent(new Event("scroll", { bubbles: true }));
      });
      await assertDocked();
    });
  });
});
