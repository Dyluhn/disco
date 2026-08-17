/**
 * discoHarness — Playwright test fixture that wires Recorder + wsRecorder.
 *
 * Provides a `recorder` fixture to every spec that imports from this module.
 * The recorder:
 *   - Starts before the test body (a unique run folder is created).
 *   - Captures network requests/responses, console messages, page errors, and
 *     WebSocket frames throughout the test.
 *   - Calls recorder.dossier() in teardown to ensure the run folder is always
 *     complete even if the test crashes before an explicit dossier() call.
 *
 * Usage in specs:
 *   import { test, expect } from "../fixtures/discoHarness";
 *
 * evidence-harness-campaign.md W11
 */

import { test as base } from "@playwright/test";
import { Recorder } from "../support/recorder";
import { attachWsRecorder } from "../support/wsRecorder";

// ---------------------------------------------------------------------------
// Fixture types
// ---------------------------------------------------------------------------

export interface DiscoHarnessFixtures {
  /** Active Recorder for the current test run. */
  recorder: Recorder;
}

// ---------------------------------------------------------------------------
// Extended test object
// ---------------------------------------------------------------------------

export const test = base.extend<DiscoHarnessFixtures>({
  recorder: async ({ page }, provide, testInfo) => {
    // Derive a deterministic, filesystem-safe run-id from the test title.
    const safeTitle = testInfo.title
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 60);
    const ts = new Date()
      .toISOString()
      .replace(/[^0-9T]/g, "")
      .slice(0, 15);
    const recorder = new Recorder(`${ts}-${safeTitle}`);

    // Wire WS recording (must be before navigation).
    attachWsRecorder(page, recorder);

    // Record HTTP request/response pairs (url + status; no bodies to avoid secrets).
    page.on("request", (req) => {
      recorder.write("network.jsonl", {
        event: "request",
        method: req.method(),
        url: req.url(),
        ts: new Date().toISOString(),
      });
    });

    page.on("response", (res) => {
      recorder.write("network.jsonl", {
        event: "response",
        status: res.status(),
        url: res.url(),
        ts: new Date().toISOString(),
      });
    });

    // Record browser console output.
    page.on("console", (msg) => {
      recorder.write("console.jsonl", {
        type: msg.type(),
        text: msg.text(),
        ts: new Date().toISOString(),
      });
    });

    // Record uncaught page errors.
    page.on("pageerror", (err) => {
      recorder.write("page-errors.jsonl", {
        message: err.message,
        ts: new Date().toISOString(),
      });
    });

    // Hand the recorder to the test body.
    await provide(recorder);

    // Teardown: guarantee the dossier is written even if the test body crashed.
    recorder.dossier();
  },
});

// Re-export expect so specs can import both from this single module.
export { expect } from "@playwright/test";
