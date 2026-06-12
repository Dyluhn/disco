import { expect, test } from "@playwright/test";

/**
 * RP-07 — the report export controls render HONESTLY. MD download works now;
 * PDF/DOCX need the sandbox image rebuild (pandoc + WeasyPrint), so they are
 * visibly DISABLED with a "arrive with the next sandbox update" note — never
 * clickable buttons that 500. (no false affordances)
 */
test.describe("Deep Research → export controls", () => {
  test("MD active, PDF/DOCX disabled-with-note until the image rebuild", async ({
    page,
  }) => {
    await page.goto("/");
    await page.getByRole("button", { name: /^scope:/i }).click();
    await page.getByRole("menuitem", { name: /deep research/i }).click();
    const input = page.getByPlaceholder(/ask a research question/i);
    await input.fill("What is the current state of solid-state battery commercialization?");
    await input.press("Enter");
    await page.getByRole("button", { name: /approve & build/i }).click();

    // Report assembled → export controls appear.
    const md = page.getByRole("button", { name: /^MD$/ });
    await expect(md).toBeVisible();
    await expect(md).toBeEnabled();

    const pdf = page.getByRole("button", { name: /^PDF$/ });
    const docx = page.getByRole("button", { name: /^DOCX$/ });
    // The load-bearing assertion: PDF/DOCX are NOT clickable affordances yet.
    await expect(pdf).toBeDisabled();
    await expect(docx).toBeDisabled();
    await expect(page.getByText(/arrive with the next sandbox update/i)).toBeVisible();

    // Screenshot the control row for the evidence record.
    const row = md.locator("xpath=ancestor::div[1]");
    await row.screenshot({ path: "e2e/_artifacts/rp-07-export-controls.png" });
  });
});
