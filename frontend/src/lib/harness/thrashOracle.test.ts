import { describe, expect, it } from "vitest";

import { assessAgentErrorThrash, normalizeAgentError } from "./thrashOracle";

describe("assessAgentErrorThrash", () => {
  it("accepts at most five distinct standalone execution errors", () => {
    const result = assessAgentErrorThrash(
      Array.from({ length: 5 }, (_, index) => ({
        kind: "agent_error",
        error: `distinct failure ${index}`,
      })),
    );
    expect(result).toMatchObject({ total: 5, repeated: [], violations: [] });
  });

  it("rejects the repeated no-free-preview-port pattern retained from live-17", () => {
    const error =
      "no free platform preview port found after checking 4 candidate(s): [3000, 8080, 5000, 4321]";
    const result = assessAgentErrorThrash(
      Array.from({ length: 16 }, () => ({ kind: "agent_error", error })),
    );
    expect(result.total).toBe(16);
    expect(result.repeated).toEqual([{ error, count: 16 }]);
    expect(result.violations).toEqual([
      "agent_error total 16 exceeds 5",
      `agent_error repeated 16 times: ${error}`,
    ]);
  });

  it("normalizes volatile workspace and event ids before repeat accounting", () => {
    const first =
      "failed in /tmp/disco-sbx-root/sbx_a1/asset for evt_abc123\npermission denied";
    const second =
      "failed in /tmp/disco-sbx-root/sbx_b2/asset for evt_def456 permission   denied";
    expect(normalizeAgentError(first)).toBe(
      "failed in <workspace>/asset for <id> permission denied",
    );
    expect(assessAgentErrorThrash([
      { kind: "agent_error", error: first },
      { kind: "agent_error", error: second },
    ]).repeated).toEqual([
      { error: "failed in <workspace>/asset for <id> permission denied", count: 2 },
    ]);
  });

  it("caps varying failures that evade exact repeat matching", () => {
    const result = assessAgentErrorThrash(
      Array.from({ length: 6 }, (_, index) => ({
        kind: "agent_error",
        error: `session-${index} failed differently`,
      })),
    );
    expect(result.repeated).toEqual([]);
    expect(result.violations).toEqual(["agent_error total 6 exceeds 5"]);
  });

  it("ignores other event kinds", () => {
    expect(
      assessAgentErrorThrash([
        { kind: "observation", error: "same" },
        { kind: "status", detail: "STUCK" },
      ]),
    ).toEqual({ total: 0, repeated: [], violations: [] });
  });
});
