import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e-live",
  timeout: 720_000,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:5173", viewport: { width: 1280, height: 800 } },
  projects: [{ name: "firefox", use: { ...devices["Desktop Firefox"] } }],
});
