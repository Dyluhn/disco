/**
 * smoke.full.spec.ts — W11 smoke spec.
 *
 * ONE test that:
 *   1. Opens the app root through the reverse-tunnel (http://127.0.0.1:5173).
 *   2. Records network/WS/console automatically via the discoHarness fixture.
 *   3. Takes a screenshot.
 *   4. Writes the dossier.
 *   5. Asserts the page title is non-empty and the dossier folder was produced
 *      with all three required files (manifest.json, timeline.md, index.html).
 *
 * This spec is designed to run on VM 201 by the orchestrator; it cannot run
 * on the workstation due to the memory cap on Playwright processes.
 *
 * evidence-harness-campaign.md W11
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { expect, test } from "../fixtures/discoHarness";

test("smoke — open home page and produce a dossier", async ({
  page,
  recorder,
}) => {
  // -------------------------------------------------------------------------
  // 1. Navigate
  // -------------------------------------------------------------------------
  await page.goto("/");

  // -------------------------------------------------------------------------
  // 2. Record UI action
  // -------------------------------------------------------------------------
  recorder.write("ui-actions.jsonl", {
    action: "navigate",
    url: page.url(),
    ts: new Date().toISOString(),
  });

  // -------------------------------------------------------------------------
  // 3. Screenshot into the run folder
  // -------------------------------------------------------------------------
  const screenshotsDir = path.join(recorder.runDir, "screenshots");
  fs.mkdirSync(screenshotsDir, { recursive: true });
  await page.screenshot({
    path: path.join(screenshotsDir, "home.png"),
    fullPage: false,
  });

  // -------------------------------------------------------------------------
  // 4. Write dossier (before assertions so we can assert on the files)
  // -------------------------------------------------------------------------
  recorder.dossier();

  // -------------------------------------------------------------------------
  // 5. Assertions
  // -------------------------------------------------------------------------

  // UI truth: the app loaded a non-empty page title.
  const title = await page.title();
  expect(title.length).toBeGreaterThan(0);

  // Dossier truth: the run folder and all three required files exist.
  expect(fs.existsSync(recorder.runDir)).toBe(true);
  expect(
    fs.existsSync(path.join(recorder.runDir, "manifest.json")),
  ).toBe(true);
  expect(
    fs.existsSync(path.join(recorder.runDir, "timeline.md")),
  ).toBe(true);
  expect(
    fs.existsSync(path.join(recorder.runDir, "index.html")),
  ).toBe(true);
});
