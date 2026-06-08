import { expect, test } from "@playwright/test";

test.describe("Research → grounded answer", () => {
  test("a query renders a cited answer with sources", async ({ page }) => {
    await page.goto("/");

    const input = page.getByPlaceholder(/ask anything/i);
    await expect(input).toBeVisible();

    await input.fill(
      "How does reciprocal rank fusion work, and when should I use it?",
    );
    await input.press("Enter");

    // The submitted query becomes the answer's heading.
    await expect(
      page.getByRole("heading", { name: /reciprocal rank fusion/i }),
    ).toBeVisible();

    // The source panel exposes a Cited tab (citations come only from passages).
    await expect(page.getByRole("tab", { name: /cited/i })).toBeVisible();
  });
});
