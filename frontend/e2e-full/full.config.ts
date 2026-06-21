import { defineConfig, devices } from "@playwright/test";

/**
 * Evidence harness Playwright config — W11 (Lane E).
 *
 * This config is intentionally free of `webServer` auto-launch.
 * Prerequisites (managed externally before running):
 *   1. Workstation: `vite` dev server on :5173, agent-server on :8000.
 *   2. Reverse SSH tunnel from VM 201 so :5173/:8000 land on VM 201's localhost.
 *
 * Run on VM 201:
 *   npx playwright test --config frontend/e2e-full/full.config.ts
 *
 * Artifacts land under test-record/e2e-full/ (relative to the frontend CWD).
 */
export default defineConfig({
  testDir: "./scenarios",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  /** Playwright's own trace/screenshot artifacts (separate from the Recorder dossier). */
  outputDir: "../test-record/e2e-full/pw-output",
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "on",
    screenshot: "on",
    video: "off",
    viewport: { width: 1280, height: 800 },
  },
  projects: [
    {
      name: "live",
      use: { ...devices["Desktop Firefox"] },
    },
  ],
});
