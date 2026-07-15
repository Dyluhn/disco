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
// Keep the local UI on the SAME site as the default live APIs (127.0.0.1).
// `localhost` and `127.0.0.1` are different SameSite identities; mixing them
// makes the Strict session cookie disappear after an apparently-successful
// pairing and turns every protected live request into a 401.
const BASE_URL = REMOTE || `http://127.0.0.1:${PORT}`;
const LIVE_WORKERS = Math.max(1, Number(process.env.LIVE_WORKERS ?? 1));
const RELIABILITY_AGENT_URL =
  process.env.DISCO_RELIABILITY_AGENT_URL ?? "http://127.0.0.1:8000";
const RELIABILITY_APP_URL =
  process.env.DISCO_RELIABILITY_APP_URL ?? "http://127.0.0.1:8800";

export default defineConfig({
  testDir: "./e2e-live",
  fullyParallel: LIVE_WORKERS > 1,
  workers: LIVE_WORKERS,
  retries: 0,
  timeout: 900_000, // a live agent run on the local 27B is slow — budget 15 min
  expect: { timeout: 30_000 },
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
    // Authenticated traces faithfully retain cookies and response metadata.
    // Playwright has no header/body redaction hook, so live evidence uses
    // screenshots, JSON results, DISCO_INSPECT, and provider-ledger records.
    trace: "off",
    viewport: { width: 1280, height: 800 },
  },
  projects: [{ name: "firefox", use: { ...devices["Desktop Firefox"] } }],
  // Remote mode brings its own served UI; only local mode spins a vite dev server.
  ...(REMOTE
    ? {}
    : {
        webServer: {
          command: `npm run dev -- --host 127.0.0.1 --port ${PORT} --strictPort`,
          url: BASE_URL,
          reuseExistingServer: false,
          timeout: 60_000,
          // Shell env has precedence over ignored local Vite dotfiles. This
          // keeps browser/API hosts same-site and makes the suite topology
          // explicit instead of inheriting a developer's localhost override.
          env: {
            VITE_AGENT_BASE: "/svc/agent",
            VITE_API_BASE: "/svc/app",
            DISCO_VITE_AGENT_PROXY_TARGET: RELIABILITY_AGENT_URL,
            DISCO_VITE_APP_PROXY_TARGET: RELIABILITY_APP_URL,
          },
        },
      }),
});
