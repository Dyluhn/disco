import { join } from "node:path";

import { afterEach, describe, expect, test, vi } from "vitest";

const ISOLATED = "DISCO_RELIABILITY_ISOLATED_STACK";
const SUITE_OUT = "DISCO_RELIABILITY_SUITE_OUT";

async function loadConfig(options: {
  isolated?: string;
  suiteOut?: string;
}) {
  vi.resetModules();
  vi.stubEnv(ISOLATED, options.isolated ?? "");
  vi.stubEnv(SUITE_OUT, options.suiteOut ?? "");
  return (await import("../../playwright.live.config")).default;
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("live Playwright reliability evidence output", () => {
  test("puts each isolated suite's artifacts below its own evidence directory", async () => {
    const firstSuite = "/tmp/campaign/live-search-grounding";
    const secondSuite = "/tmp/campaign/live-search-deep";

    const first = await loadConfig({ isolated: "1", suiteOut: firstSuite });
    const second = await loadConfig({ isolated: "true", suiteOut: secondSuite });

    expect(first.outputDir).toBe(join(firstSuite, "playwright-artifacts"));
    expect(second.outputDir).toBe(join(secondSuite, "playwright-artifacts"));
    expect(first.outputDir).not.toBe(second.outputDir);
  });

  test("fails closed when an isolated suite has no evidence directory", async () => {
    await expect(loadConfig({ isolated: "1" })).rejects.toThrow(
      "isolated live reliability requires DISCO_RELIABILITY_SUITE_OUT",
    );
  });

  test("rejects a relative suite output path", async () => {
    await expect(
      loadConfig({ isolated: "yes", suiteOut: "relative/evidence" }),
    ).rejects.toThrow(
      "DISCO_RELIABILITY_SUITE_OUT must be an absolute path",
    );
  });

  test("preserves Playwright's default for non-campaign local runs", async () => {
    const config = await loadConfig({});

    expect(config.outputDir).toBeUndefined();
  });
});
