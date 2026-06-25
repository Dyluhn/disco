import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    // The lifecycle test spawns a real sidecar subprocess and waits on a
    // heartbeat, so give individual tests headroom.
    testTimeout: 20_000,
    hookTimeout: 20_000,
  },
});
