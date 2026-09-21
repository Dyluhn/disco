import { expect, test } from "@playwright/test";

/**
 * RP-07 — the report export controls render HONESTLY. MD download works now;
 * PDF is gated on the real server capability (/api/export/capabilities) —
 * weasyprint. Offline/fixture mode has no backend, so the capability fetch fails
 * and defaults to md-only: PDF is visibly DISABLED with a "needs ... on the
 * server" note — never a clickable button that 500s. (W-12 removed DOCX export
 * end to end, so only MD + PDF render now.)
 */
test.describe("Deep Research → export controls", () => {
  test("MD active, PDF disabled-with-note (offline capability default)", async ({
    page,
  }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "Deep Research" }).click();
    const input = page.getByPlaceholder(/ask a research question/i);
    await input.fill("What is the current state of solid-state battery commercialization?");
    await input.press("Enter");
    // Research starts immediately; there is no plan-approval gate.
    await expect(page.locator("[data-dr-brief]")).toBeVisible();
    await expect(page.locator('[data-disco-control="approve-plan"]')).toHaveCount(0);

    // Report assembled → export controls appear.
    const md = page.getByRole("button", { name: /^MD$/ });
    await expect(md).toBeVisible();
    await expect(md).toBeEnabled();

    const pdf = page.getByRole("button", { name: /^PDF$/ });
    // The essential assertion: PDF is NOT a clickable affordance yet.
    await expect(pdf).toBeDisabled();
    // W-12: DOCX export was removed end to end — no DOCX control renders.
    await expect(page.getByRole("button", { name: /^DOCX$/ })).toHaveCount(0);
    await expect(page.getByText(/need(s)? .*server/i)).toBeVisible();

    // Screenshot the control row for the evidence record.
    const row = md.locator("xpath=ancestor::div[1]");
    await row.screenshot({ path: "e2e/_artifacts/rp-07-export-controls.png" });
  });
});
