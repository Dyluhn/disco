import { expect, test } from "@playwright/test";
import * as fs from "node:fs";

const AGENT_API = (
  process.env.DISCO_RELIABILITY_AGENT_URL ?? "http://127.0.0.1:8000"
).replace(/\/$/, "");
const RESTORED_CID = process.env.DISCO_RELIABILITY_RESTORED_CID ?? "";
const RESTORED_MARKER =
  process.env.DISCO_RELIABILITY_RESTORED_MARKER ?? "BP02 preview truth";
const NEW_MARKER = "P2E RESTORED NEW OPERATION";

async function approvePlan(
  page: import("@playwright/test").Page,
): Promise<void> {
  const approve = page.locator('[data-disco-control="approve-plan"]').first();
  await expect(approve).toBeVisible({ timeout: 300_000 });
  await approve.click();
}

test("restored self-host reads an existing project and completes a new Build", async ({
  page,
}, testInfo) => {
  test.setTimeout(1_200_000);
  expect(RESTORED_CID, "DISCO_RELIABILITY_RESTORED_CID is required").toMatch(
    /^conv_[a-f0-9]+$/,
  );

  const health = await page.request.get(`${AGENT_API}/health`);
  expect(health.ok(), "restored agent-server is not healthy").toBe(true);

  // Read the durable project through the real resumed Build surface and preview,
  // not by inspecting the backup archive or the volume directly.
  await page.goto(`/build/${RESTORED_CID}`);
  await page
    .getByRole("tab", { name: /preview/i })
    .first()
    .click();
  await expect(
    page.frameLocator("iframe").first().locator("body"),
  ).toContainText(RESTORED_MARKER, { timeout: 120_000 });
  await page.screenshot({
    path: testInfo.outputPath("restored-existing-project.png"),
    fullPage: true,
  });

  // Now prove the restored installation is writable and executable, not merely
  // readable: start a distinct conversation, approve its real plan, finish, and
  // render its persisted preview.
  let newCid = "";
  page.on("request", (request) => {
    const match = request.url().match(/\/conversations\/(conv_[a-f0-9]+)\//);
    if (match && match[1] !== RESTORED_CID) newCid = match[1];
  });
  await page.getByRole("link", { name: "New" }).click();
  await page.getByRole("radio", { name: "build" }).click();
  const input = page.getByPlaceholder(/describe what you want/i);
  await expect(input).toBeVisible({ timeout: 30_000 });
  await input.fill(
    `Create a static index.html whose visible h1 is exactly '${NEW_MARKER}'. ` +
      "Serve it, verify the visible page, and finish.",
  );
  await input.press("Enter");
  await approvePlan(page);
  await expect
    .poll(() => newCid, { timeout: 60_000 })
    .toMatch(/^conv_[a-f0-9]+$/);

  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `${AGENT_API}/conversations/${newCid}/state`,
        );
        expect(response.ok()).toBe(true);
        return String((await response.json()).execution_status ?? "");
      },
      { timeout: 900_000, intervals: [2_000] },
    )
    .toBe("FINISHED");

  await page
    .getByRole("tab", { name: /preview/i })
    .first()
    .click();
  await expect(
    page.frameLocator("iframe").first().locator("body"),
  ).toContainText(NEW_MARKER, { timeout: 120_000 });
  await page.screenshot({
    path: testInfo.outputPath("restored-new-build-preview.png"),
    fullPage: true,
  });
  fs.writeFileSync(
    testInfo.outputPath("lifecycle-result.json"),
    `${JSON.stringify({ restored_cid: RESTORED_CID, new_cid: newCid, status: "PASS" }, null, 2)}\n`,
    { encoding: "utf-8", mode: 0o600 },
  );
});
