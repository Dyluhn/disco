import { defineConfig, devices } from "@playwright/test";

/**
 * LIVE acceptance config (work-order pack, docs/workorders/README.md rule 2).
 *
 * Unlike playwright.config.ts (hermetic fixture mode on :5199), this config drives
 * the REAL stack: a dev server in normal mode (so `.env.development.local` /
 * VITE_AGENT_BASE points at the live agent-server on 127.0.0.1:8000) and a live
 * driver model. Nothing here is deterministic — these specs assert that real
 * agent behavior reached the real UI, and they save screenshots as evidence.
 *
 *  - Port 5174: never collide with Dylan's own dev server on 5173, and never
 *    reuse a stray server whose env we can't vouch for.
 *  - Generous timeouts: a live build turn on the local 27B takes minutes.
 *  - Firefox only: headless chromium can't rasterize text on this host.
 *
 * Run: npx playwright test --config playwright.live.config.ts <spec>
 * Precondition: agent-server up on 127.0.0.1:8000 (the spec fails fast if not).
 */
// LIVE_PORT lets the gauntlet run several browsers concurrently on distinct
// ports (5174/5175/5176 …) against the SAME live servers — parallel DR lanes.
// LIVE_BASE_URL targets a REMOTE deploy instead (e.g. http://100.81.82.115:8088):
// the remote serves its own packaged UI, so no local vite webServer is spawned.
// Remote fleet lanes = one Disco stack per host, each with its own engine.
const PORT = Number(process.env.LIVE_PORT ?? 5174);
const REMOTE = (process.env.LIVE_BASE_URL ?? "").replace(/\/$/, "");
const BASE_URL = REMOTE || `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "./e2e-live",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 900_000, // a live agent run on the local 27B is slow — budget 15 min
  expect: { timeout: 30_000 },
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 800 },
  },
  projects: [{ name: "firefox", use: { ...devices["Desktop Firefox"] } }],
  // Remote mode brings its own served UI; only local mode spins a vite dev server.
  ...(REMOTE
    ? {}
    : {
        webServer: {
          command: `npm run dev -- --port ${PORT} --strictPort`,
          url: BASE_URL,
          reuseExistingServer: false,
          timeout: 60_000,
        },
      }),
});
