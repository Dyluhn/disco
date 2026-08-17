import { realpathSync } from "node:fs";
import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

// A1 — the in-frame selection-agent IIFE has a SINGLE source of truth: the
// co-located agent-server package asset `selection_agent.js`. The frontend reads
// the very same bytes via a `?raw` import (selectionAgent.ts) so the script the
// browser runs and the script the preview-edit route injects can never drift.
// The file lives outside `current/frontend/src`, so it is aliased here and allowed
// through Vite's dev-server filesystem jail below.
const SELECTION_AGENT_JS = resolve(
  __dirname,
  "../packages/agent-server/src/disco/agent_server/selection_agent.js",
);
const ELEMENT_MENTION_PICKER_JS = resolve(
  __dirname,
  "../packages/agent-server/src/disco/agent_server/element_mention_picker.js",
);
const RELIABILITY_APP_PROXY = process.env.DISCO_VITE_APP_PROXY_TARGET?.trim();
const RELIABILITY_AGENT_PROXY = process.env.DISCO_VITE_AGENT_PROXY_TARGET?.trim();
const DEPENDENCY_ROOT = realpathSync(resolve(__dirname, "node_modules"));

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: [
      // Regex form so the alias matches `@selection-agent-script` AND preserves
      // any query suffix (`?raw`) — a plain string alias matches the whole id
      // (including the query) and would miss `@selection-agent-script?raw`.
      {
        find: /^@selection-agent-script(\?.*)?$/,
        replacement: `${SELECTION_AGENT_JS}$1`,
      },
      {
        find: /^@element-mention-picker-script(\?.*)?$/,
        replacement: `${ELEMENT_MENTION_PICKER_JS}$1`,
      },
      { find: "@", replacement: resolve(__dirname, "src") },
    ],
  },
  server: {
    fs: { allow: [resolve(__dirname, ".."), __dirname, DEPENDENCY_ROOT] },
    ...(RELIABILITY_APP_PROXY && RELIABILITY_AGENT_PROXY
      ? {
          // Live reliability uses the same single-front-door shape as the
          // packaged nginx deployment. Browser cookies, CORS, CSRF, HTTP, and
          // WebSockets therefore exercise one origin even when the local App
          // and Agent servers listen on separate ports.
          proxy: {
            "/svc/app": {
              target: RELIABILITY_APP_PROXY,
              changeOrigin: true,
              headers: { origin: new URL(RELIABILITY_APP_PROXY).origin },
              rewrite: (path: string) => path.replace(/^\/svc\/app/, ""),
            },
            "/svc/agent": {
              target: RELIABILITY_AGENT_PROXY,
              changeOrigin: true,
              ws: true,
              headers: { origin: new URL(RELIABILITY_AGENT_PROXY).origin },
              rewrite: (path: string) => path.replace(/^\/svc\/agent/, ""),
            },
          },
        }
      : {}),
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    // The Playwright E2E specs (e2e/*.spec.ts, e2e-live/*.spec.ts, e2e-full/*.spec.ts,
    // and e2e-policy/*.spec.ts)
    // are NOT vitest tests — they run under `npm run test:e2e` / the VM-201 evidence
    // tier. Without this, vitest picks them up and they fail with "Playwright Test did
    // not expect test() to be called here". (e2e-full was added by the evidence-harness
    // campaign but missed here — restored with the per-surface UI-control specs.)
    exclude: [
      ...configDefaults.exclude,
      "e2e/**",
      "e2e-live/**",
      "e2e-full/**",
      "e2e-policy/**",
    ],
  },
});
