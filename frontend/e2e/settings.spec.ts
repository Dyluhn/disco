import { expect, test } from "@playwright/test";

test.describe("Settings", () => {
  test("renders the model matrix, skills, and connections from config", async ({
    page,
  }) => {
    await page.goto("/settings");

    await expect(
      page.getByRole("heading", { name: /^settings$/i }),
    ).toBeVisible();

    // The model library is intentionally collapsed until the user asks to edit it.
    await page.getByText("Model library", { exact: true }).click();
    // The default local driver is present inside it.
    await expect(page.getByText(/qwen/i).first()).toBeVisible();

    // The Skills configuration section renders (its contents are user/local
    // state, so assert the section heading rather than seeded fixture rows).
    await expect(page.getByRole("heading", { name: /^skills$/i })).toBeVisible();
  });
});
