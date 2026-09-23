import { expect, test } from "@playwright/test";

test("model request options save and reopen without vendor recognition", async ({ page }) => {
  await page.goto("/settings");
  await page.locator("#model-library summary").first().click();
  await page.getByRole("button", { name: "Add model", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Catalogue id", { exact: true }).fill("arbitrary-endpoint");
  await dialog.getByLabel("Model id (sent to the API)", { exact: true }).fill("unseen-model-9000");
  await dialog.getByLabel("Endpoint base URL", { exact: true }).fill("https://unknown.example/v1");
  await dialog.getByText("Advanced model metadata", { exact: true }).click();
  const options = dialog.getByRole("textbox", { name: /Request options/ });
  await options.fill("invalid");
  await dialog.getByRole("button", { name: "Add model", exact: true }).click();
  await expect(dialog.getByRole("alert")).toHaveText("Request options must be a JSON object.");
  const policy = { reasoning_disabled: { reasoning_effort: "none" } };
  await options.fill(JSON.stringify(policy, null, 2));
  await dialog.getByRole("button", { name: "Add model", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole("button", { name: "Edit arbitrary-endpoint", exact: true }).click();
  await dialog.getByText("Advanced model metadata", { exact: true }).click();
  const savedOptions = dialog.getByRole("textbox", { name: /Request options/ });
  await expect(savedOptions).toHaveValue(JSON.stringify(policy, null, 2));
  await savedOptions.scrollIntoViewIfNeeded();
  await page.screenshot({ path: process.env.DISCO_POLICY_SCREENSHOT ?? "/tmp/disco-request-policy.png" });
});
