import { expect, test } from "@playwright/test";

test("deck element accessible names stay current through edit and history transitions", async ({
  page,
}) => {
  await page.goto("/e2e/fixtures/deck-editor-a11y.html");

  const layoutLabel = page.getByLabel("Layout accessible name");
  const element = (name: string) => page.getByRole("button", { name, exact: true });

  await expect(element("title: Original title")).toBeVisible();
  await expect(layoutLabel).toHaveText("title: Original title");

  // Keyboard-only selection + edit. Role/name queries exercise the same accessible
  // names a screen reader exposes rather than implementation-only text selectors.
  await element("title: Original title").focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  const editor = page.getByRole("textbox", { name: "Edit title" });
  await editor.fill("Committed title");
  await page.keyboard.press("Enter");
  await expect(element("title: Committed title")).toBeVisible();
  await expect(layoutLabel).toHaveText("title: Committed title");

  await page.getByRole("button", { name: "Undo", exact: true }).click();
  await expect(element("title: Original title")).toBeVisible();
  await expect(layoutLabel).toHaveText("title: Original title");

  await page.getByRole("button", { name: "Redo", exact: true }).click();
  await expect(element("title: Committed title")).toBeVisible();
  await expect(layoutLabel).toHaveText("title: Committed title");

  await page.getByRole("button", { name: "Reorder slides", exact: true }).click();
  await expect(element("title: Second title")).toBeVisible();
  await expect(layoutLabel).toHaveText("title: Second title");

  await page.getByRole("button", { name: "Duplicate current slide", exact: true }).click();
  await page.getByLabel("Slide strip").getByRole("button", { name: "Slide 2" }).click();
  await expect(element("title: Second title copy")).toBeVisible();

  await page.reload();
  await page.getByLabel("Slide strip").getByRole("button", { name: "Slide 2" }).click();
  await expect(element("title: Second title copy")).toBeVisible();
});
