import { defineConfig } from "@playwright/test";

import liveConfig from "./playwright.live.config";

if (liveConfig.use?.trace !== "off") {
  throw new Error(
    "authenticated live Playwright trace policy must be exactly off",
  );
}

/**
 * Executes an intentional authenticated-browser failure using the production
 * live artifact policy. The wrapper asserts the browser reached the failure and
 * that Playwright emitted no trace archive.
 */
export default defineConfig({
  ...liveConfig,
  testDir: "./e2e-policy",
  testMatch: "live-trace-off.spec.ts",
  outputDir: process.env.DISCO_TRACE_POLICY_OUTPUT,
  retries: 0,
  workers: 1,
  reporter: [["line"]],
  webServer: undefined,
});
