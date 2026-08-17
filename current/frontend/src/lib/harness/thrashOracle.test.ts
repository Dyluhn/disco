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

  it("does not merge different shell actions that share a generic exit code", () => {
    const result = assessAgentErrorThrash([
      {
        id: "act-one",
        kind: "action",
        tool_call: {
          tool_name: "shell",
          arguments: { command: "curl /health" },
        },
      },
      { kind: "agent_error", action_id: "act-one", error: "command exited 1" },
      {
        id: "act-two",
        kind: "action",
        tool_call: {
          tool_name: "shell",
          arguments: { command: "grep marker index.html" },
        },
      },
      { kind: "agent_error", action_id: "act-two", error: "command exited 1" },
    ]);

    expect(result).toEqual({ total: 2, repeated: [], violations: [] });
  });

  it("rejects the same normalized action failing under different action ids", () => {
    const result = assessAgentErrorThrash([
      {
        id: "act-one",
        kind: "action",
        tool_call: {
          tool_name: "shell",
          arguments: { timeout: 5, command: "curl /health" },
        },
      },
      { kind: "agent_error", action_id: "act-one", error: "command exited 1" },
      {
        id: "act-two",
        kind: "action",
        tool_call: {
          tool_name: "shell",
          arguments: { command: "curl /health", timeout: 5 },
        },
      },
      { kind: "agent_error", action_id: "act-two", error: "command exited 1" },
    ]);

    expect(result.repeated).toEqual([{ error: "command exited 1", count: 2 }]);
    expect(result.violations).toEqual([
      "agent_error repeated 2 times: command exited 1",
    ]);
  });

  it("normalizes volatile workspace and call ids inside paired action arguments", () => {
    const result = assessAgentErrorThrash([
      {
        id: "action-one",
        kind: "action",
        tool_call: {
          tool_name: "shell",
          arguments: {
            command: "cat /tmp/disco-sbx-root/sbx_first/output-call_alpha.txt",
          },
        },
      },
      { kind: "agent_error", action_id: "action-one", error: "command exited 1" },
      {
        id: "action-two",
        kind: "action",
        tool_call: {
          tool_name: "shell",
          arguments: {
            command: "cat /tmp/disco-sbx-root/sbx_second/output-call_beta.txt",
          },
        },
      },
      { kind: "agent_error", action_id: "action-two", error: "command exited 1" },
    ]);

    expect(result.repeated).toEqual([{ error: "command exited 1", count: 2 }]);
    expect(result.violations).toEqual([
      "agent_error repeated 2 times: command exited 1",
    ]);
  });

  it("keeps missing action correlation fail-closed", () => {
    const result = assessAgentErrorThrash([
      {
        kind: "agent_error",
        action_id: "missing-one",
        error: "command exited 1",
      },
      {
        kind: "agent_error",
        action_id: "missing-two",
        error: "command exited 1",
      },
    ]);

    expect(result.violations).toEqual([
      "agent_error repeated 2 times: command exited 1",
    ]);
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
