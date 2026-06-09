import { expect, test } from "@playwright/test";

// Visual + functional proof of the two-way Ask-gate in the REAL app (offline
// fixture demo: a task containing "ask" routes to a free-form ask_user question).
test.describe("Build → free-form Ask-gate", () => {
  test("the agent's question pauses with an AskPanel; answering resumes", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("radio", { name: "build" }).click();

    const input = page.getByPlaceholder(/describe what you want/i);
    await expect(input).toBeVisible();
    await input.fill("Build a fizzbuzz, but ask me about the output format first");
    await input.press("Enter");

    // The two-way Ask-gate: the agent's free-form question parks the run.
    const ask = page.getByRole("alertdialog", { name: /question for you/i });
    await expect(ask).toBeVisible();
    await expect(ask.getByText(/stdout/i)).toBeVisible();
    const answerBox = ask.getByRole("textbox", { name: /answer the agent/i });
    await expect(answerBox).toBeVisible();

    // SCREENSHOT 1: the Ask-gate open, question + answer box visible.
    await page.screenshot({ path: "e2e/_artifacts/ask-gate-open.png", fullPage: true });

    // Type an answer and send it — the run resumes.
    await answerBox.fill("stdout is fine");
    await answerBox.press("Enter");

    // The gate clears and the run finishes.
    await expect(ask).toBeHidden();
    await expect(page.getByText(/fizzbuzz ran correctly/i)).toBeVisible();

    // SCREENSHOT 2: resumed + finished after answering.
    await page.screenshot({ path: "e2e/_artifacts/ask-gate-answered.png", fullPage: true });
  });
});
