import {
  expect,
  request as playwrightRequest,
  test,
  type APIRequestContext,
} from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";

import {
  AGENT_API,
  allEvents,
  assertNoThrash,
  authenticatedMutation,
  deleteConversation,
  getJson,
  inspectTrace,
  requireReliabilityStack,
  waitForStatus,
} from "./reliability-helpers";

const INITIAL_MARKER = "LIFECYCLE ORIGINAL";
const REVISED_MARKER = "LIFECYCLE REVISED";
const STEER_MARKER = "STEER WAS APPLIED";
const FINAL_MARKER = "POST ROLLBACK CONTINUATION";
const ISOLATED_PREVIEW_PREFIX = "/__disco/isolated-preview";

// Preview redemption bodies contain a one-time bearer. Keep them out of
// retained failure traces and compare bearer-sensitive values as booleans.
test.use({ trace: "off" });

async function isolatedPreviewText(
  request: APIRequestContext,
  cid: string,
  version?: number,
): Promise<string> {
  const targetPath = version === undefined ? "/" : `/?version=${version}`;
  const mint = await authenticatedMutation(
    request,
    `${AGENT_API}/conversations/${cid}/preview/capability`,
    {
      data: { port: 8000, target_path: targetPath, transport: "path" },
      timeout: 30_000,
    },
  );
  expect(mint.ok(), `preview capability mint returned ${mint.status()}`).toBe(true);
  expect(mint.headers()["cache-control"]).toContain("no-store");
  const capability = (await mint.json()) as {
    bootstrap_url: string;
    bootstrap_intent: string;
    target_path: string;
    transport: string;
  };
  expect(capability.target_path).toBe(targetPath);
  expect(capability.transport).toBe("path");
  expect(
    typeof capability.bootstrap_intent === "string" && capability.bootstrap_intent.length > 0,
    "path capability response omitted its one-time intent",
  ).toBe(true);
  const bootstrapUrl = new URL(capability.bootstrap_url);
  expect(
    bootstrapUrl.search === "" && bootstrapUrl.hash === "",
    "path bootstrap URL contained query or fragment data",
  ).toBe(true);
  expect(
    !capability.bootstrap_url.includes(capability.bootstrap_intent),
    "path bootstrap URL exposed its one-time intent",
  ).toBe(true);

  const isolated = await playwrightRequest.newContext();
  try {
    const isolatedRoot = `${bootstrapUrl.origin}${ISOLATED_PREVIEW_PREFIX}/${cid}/${
      version === undefined ? "" : `?version=${version}`
    }`;
    const deniedBeforeRedemption = await isolated.get(isolatedRoot);
    expect(deniedBeforeRedemption.status()).toBe(403);

    const redemption = await isolated.post(capability.bootstrap_url, {
      form: { intent: capability.bootstrap_intent },
      timeout: 30_000,
    });
    expect(redemption.status(), "isolated body-only capability redemption failed").toBe(200);
    expect(
      (await redemption.text()).includes(capability.bootstrap_intent),
      "bootstrap response reflected the one-time bearer",
    ).toBe(false);

    const preview = await isolated.get(isolatedRoot);
    expect(preview.ok(), `isolated preview root returned ${preview.status()}`).toBe(true);
    return await preview.text();
  } finally {
    await isolated.dispose();
  }
}

async function approvePlan(page: import("@playwright/test").Page): Promise<void> {
  const approve = page.locator('[data-disco-control="approve-plan"]').first();
  const backendError = page.getByRole("heading", { name: "Can't reach the server" });
  await Promise.race([
    approve.waitFor({ state: "visible", timeout: 300_000 }),
    backendError.waitFor({ state: "visible", timeout: 300_000 }).then(async () => {
      throw new Error(`Build surface lost its backend: ${await page.locator("body").innerText()}`);
    }),
  ]);
  await approve.click();
}

test("Build survives pause/close/resume, steers, exports, rolls back, then builds again", async ({
  context,
  page,
  request,
}, testInfo) => {
  test.setTimeout(1_800_000);
  await requireReliabilityStack(request);

  const create = await authenticatedMutation(request, `${AGENT_API}/conversations`, {
    data: { surface: "build", title: "Reliability lifecycle gauntlet" },
    timeout: 30_000,
  });
  expect(create.ok(), await create.text()).toBe(true);
  const cid = String((await create.json()).conversation_id);

  try {
    const kick = await authenticatedMutation(
      request,
      `${AGENT_API}/conversations/${cid}/messages`,
      {
        data: {
          content:
            `Build a static index.html with an h1 exactly '${INITIAL_MARKER}', ` +
            "a visible card saying 'Resume proof', a styles.css, and a script.js. " +
            "Serve it, verify it in a browser, and finish.",
        },
        timeout: 30_000,
      },
    );
    expect(kick.ok(), await kick.text()).toBe(true);

    await page.goto(`/build/${cid}`);
    await approvePlan(page);

    // Stop after the first real action. A fast finish is a lifecycle failure,
    // not a reason to skip this trial.
    const actionDeadline = Date.now() + 300_000;
    while (Date.now() < actionDeadline) {
      const events = await allEvents(request, cid);
      if (events.some((event) => event.kind === "action")) break;
      await page.waitForTimeout(500);
    }
    expect((await allEvents(request, cid)).some((event) => event.kind === "action")).toBe(true);
    await page
      .getByRole("button", { name: /stop the agent gracefully/i })
      .click({ timeout: 30_000 });
    const stopped = await waitForStatus(
      request,
      cid,
      new Set(["PAUSED", "IDLE"]),
      300_000,
    );

    // Simulate closing the desktop app while stopped, then reopening the exact
    // durable build URL and resuming from persisted state.
    await page.close();
    page = await context.newPage();
    await page.goto(`/build/${cid}`);
    await expect(page.getByText(stopped === "PAUSED" ? "Paused" : "Stopped", { exact: true }).first()).toBeVisible({
      timeout: 60_000,
    });
    const resume = page.getByRole("button", { name: /resume the agent/i });
    await expect(resume).toBeVisible({ timeout: 30_000 });
    await resume.click();
    await waitForStatus(request, cid, new Set(["RUNNING", "FINISHED"]), 120_000);
    await waitForStatus(request, cid, new Set(["FINISHED", "VERIFIED"]), 900_000);

    // FINISHED is emitted before the sandbox copy completes. Wait for the
    // product's durable version-commit event so history can never race that copy.
    const versionDeadline = Date.now() + 120_000;
    let originalSeq: number | null = null;
    while (Date.now() < versionDeadline) {
      const events = await allEvents(request, cid);
      const finishedSeq = Math.max(
        0,
        ...events
          .filter((event) => event.kind === "status" && event.status === "FINISHED")
          .map((event) => event.seq ?? 0),
      );
      const committed = events.find(
        (event) => event.kind === "workspace_version" && (event.seq ?? 0) > finishedSeq,
      );
      if (committed?.kind === "workspace_version") {
        originalSeq = committed.version_seq;
        break;
      }
      await page.waitForTimeout(250);
    }
    expect(originalSeq, "finished build emitted no durable workspace-version commit").not.toBeNull();

    const initialVersions = await getJson<{ versions: Array<{ seq: number }> }>(
      request,
      `${AGENT_API}/conversations/${cid}/versions`,
    );
    expect(initialVersions.versions.length, "initial finish saved no workspace version").toBeGreaterThan(0);
    expect(initialVersions.versions.some((version) => version.seq === originalSeq)).toBe(true);
    const historical = await isolatedPreviewText(request, cid, originalSeq!);
    expect(
      historical,
      "committed finished version did not contain the completed original build",
    ).toContain(INITIAL_MARKER);

    // Prove rendered preview and backend live/static preview agree.
    await page.getByRole("tab", { name: /preview/i }).first().click();
    expect(await isolatedPreviewText(request, cid)).toContain(INITIAL_MARKER);
    const previewBody = page.frameLocator("iframe").first().locator("body");
    await expect(previewBody).toContainText(INITIAL_MARKER, { timeout: 120_000 });

    // Export through the real UI and validate the resulting archive, not just
    // the presence of an Export button.
    const downloadPromise = page.waitForEvent("download");
    await page.locator('[data-disco-control="build.export-zip"]').click();
    const download = await downloadPromise;
    const zipPath = testInfo.outputPath(await download.suggestedFilename());
    await download.saveAs(zipPath);
    expect(fs.statSync(zipPath).size).toBeGreaterThan(100);
    expect(fs.readFileSync(zipPath).subarray(0, 2).toString("ascii")).toBe("PK");
    execFileSync("unzip", ["-t", zipPath], { timeout: 30_000, stdio: "pipe" });

    // A settled follow-up must create a new plan. Apply a live steer while that
    // revised plan is executing and require both directives in the artifact.
    const replan = page.getByPlaceholder(/plan a change to this build/i);
    await replan.fill(
      `Change the h1 to exactly '${REVISED_MARKER}' and add a section with that same text.`,
    );
    await replan.press("Enter");
    await approvePlan(page);
    await waitForStatus(request, cid, new Set(["RUNNING"]), 120_000);
    const steer = page.getByLabel("Steer the agent");
    await steer.fill(`Also add visible text exactly '${STEER_MARKER}' before finishing.`);
    await page.locator('[data-disco-control="steer"]').click();
    // A mutating mid-run steer must invalidate the stale plan. Prove the revised
    // plan reaches the real approval gate, then approve it as the user.
    await approvePlan(page);
    await waitForStatus(request, cid, new Set(["FINISHED", "VERIFIED"]), 900_000);
    let live = await isolatedPreviewText(request, cid);
    expect(live).toContain(REVISED_MARKER);
    expect(live).toContain(STEER_MARKER);

    // Select the original historical preview and use the real rollback dialog.
    await page.getByRole("tab", { name: /preview/i }).first().click();
    await page.getByRole("button", { name: "Version history" }).click();
    await page
      .getByRole("menuitem")
      .filter({ hasText: new RegExp(`v${originalSeq}\\b`) })
      .click();
    await expect(page.getByText(`viewing v${originalSeq} (read-only)`, { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Roll back to this version" }).click();
    await page
      .locator(
        '[data-confirm-context="workspace-rollback"][data-disco-control="confirm-dialog.confirm"]',
      )
      .click();
    await expect(
      page
        .getByRole("status")
        .filter({ hasText: `Rolled back to v${originalSeq}` })
        .first(),
    ).toContainText(`Rolled back to v${originalSeq}`, { timeout: 60_000 });
    live = await isolatedPreviewText(request, cid);
    expect(live).toContain(INITIAL_MARKER);
    expect(live).not.toContain(REVISED_MARKER);

    // Continue from the restored workspace. This catches rollback implementations
    // that look correct once but poison the next plan/snapshot/preview cycle.
    const postRollback = page.getByPlaceholder(/plan a change to this build/i);
    await postRollback.fill(
      `Continue from the restored version. Add visible text exactly '${FINAL_MARKER}', ` +
        "serve, verify, and finish.",
    );
    await postRollback.press("Enter");
    await approvePlan(page);
    await waitForStatus(request, cid, new Set(["FINISHED", "VERIFIED"]), 900_000);
    live = await isolatedPreviewText(request, cid);
    expect(live).toContain(INITIAL_MARKER);
    expect(live).toContain(FINAL_MARKER);

    const events = await allEvents(request, cid);
    const trace = await inspectTrace(request, cid);
    expect(events.some((event) => event.kind === "workspace_restored")).toBe(true);
    expect(events.filter((event) => event.kind === "plan").length).toBeGreaterThanOrEqual(3);
    assertNoThrash(events, trace);
    fs.writeFileSync(
      testInfo.outputPath("lifecycle-evidence.json"),
      JSON.stringify({ cid, originalSeq, events, trace }, null, 2),
      "utf-8",
    );
  } finally {
    await deleteConversation(request, cid);
  }
});
