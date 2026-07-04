import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

// A1 — the in-frame selection-agent IIFE has a SINGLE source of truth: the
// co-located agent-server package asset `selection_agent.js`. The frontend reads
// the very same bytes via a `?raw` import (selectionAgent.ts) so the script the
// browser runs and the script the preview-edit route injects can never drift.
// The file lives outside `frontend/src`, so it is aliased here and allowed
// through Vite's dev-server filesystem jail below.
const SELECTION_AGENT_JS = resolve(
  __dirname,
  "../packages/agent-server/src/disco/agent_server/selection_agent.js",
);
const ELEMENT_MENTION_PICKER_JS = resolve(
  __dirname,
  "../packages/agent-server/src/disco/agent_server/element_mention_picker.js",
);

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
    fs: { allow: [resolve(__dirname, ".."), __dirname] },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    // The Playwright E2E specs (e2e/*.spec.ts, e2e-live/*.spec.ts, e2e-full/*.spec.ts)
    // are NOT vitest tests — they run under `npm run test:e2e` / the VM-201 evidence
    // tier. Without this, vitest picks them up and they fail with "Playwright Test did
    // not expect test() to be called here". (e2e-full was added by the evidence-harness
    // campaign but missed here — restored with the per-surface UI-control specs.)
    exclude: [...configDefaults.exclude, "e2e/**", "e2e-live/**", "e2e-full/**"],
  },
});
