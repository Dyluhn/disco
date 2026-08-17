import { defineConfig, devices } from "@playwright/test";

/**
 * Phase 7 — E2E + visual regression, run against FIXTURE mode (no backend).
 *
 * Why these choices:
 *  - `vite --mode test` makes Vite skip `.env.development.local` (which points the
 *    dev server at the live tailnet stack), so the app falls back to in-repo
 *    fixtures — deterministic, hermetic, never the live build.
 *  - A dedicated port (5199) avoids colliding with a dev server you may already
 *    have on 5173, and `reuseExistingServer: false` guarantees we drive OUR
 *    fixture-mode server, not a stray live one.
 *  - Firefox only: on this box headless chromium cannot rasterize text (blank
 *    screenshots), so visual baselines must come from Firefox. Functional specs
 *    assert the DOM, not pixels, so Firefox serves both.
 */
const PORT = 5199;
const BASE_URL = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  expect: {
    timeout: 10_000,
    toHaveScreenshot: { maxDiffPixelRatio: 0.02 },
  },
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 800 },
  },
  projects: [{ name: "firefox", use: { ...devices["Desktop Firefox"] } }],
  webServer: {
    command: `npm run dev -- --mode test --port ${PORT} --strictPort`,
    url: BASE_URL,
    reuseExistingServer: false,
    timeout: 120_000,
    env: { VITE_API_BASE: "", VITE_AGENT_BASE: "" },
  },
});
