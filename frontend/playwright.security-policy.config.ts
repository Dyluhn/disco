import { defineConfig } from "@playwright/test";

import liveConfig from "./playwright.live.config";

/** Real-Firefox browser-semantics probes that require no product stack. */
export default defineConfig({
  ...liveConfig,
  testDir: "./e2e-policy",
  testMatch: "preview-cookie-toss.spec.ts",
  retries: 0,
  workers: 1,
  reporter: [["line"]],
  webServer: undefined,
});
