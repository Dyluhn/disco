import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { configDefaults, defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": resolve(__dirname, "src") },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    // The Playwright E2E specs (e2e/*.spec.ts, e2e-live/*.spec.ts) are NOT vitest
    // tests — they run under `npm run test:e2e` / playwright.live.config.ts.
    // Without this, vitest picks them up and they fail with "Playwright Test did
    // not expect test() to be called here".
    exclude: [...configDefaults.exclude, "e2e/**", "e2e-live/**"],
  },
});
