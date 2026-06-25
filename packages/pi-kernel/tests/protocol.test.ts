/**
 * Protocol parser safety (EPIC B / PR B1): the line protocol must NEVER throw on
 * malformed input — `parseCommand` returns `null` for anything that is not a
 * well-formed command, and the caller turns that into a protocol `error` frame
 * (see index.ts). These are pure, allocation-only tests (no Pi, no subprocess).
 */
import { describe, expect, it } from "vitest";

import {
  PROTOCOL_VERSION,
  encodeOutbound,
  parseCommand,
  type KernelCommand,
} from "../src/protocol.ts";

describe("parseCommand — well-formed commands", () => {
  it("accepts every inbound command type", () => {
    const cases: Array<[unknown, KernelCommand]> = [
      [{ type: "init" }, { type: "init", config: undefined }],
      [
        { type: "init", config: { conversationId: "c1", heartbeatMs: 25 } },
        { type: "init", config: { conversationId: "c1", heartbeatMs: 25 } },
      ],
      [{ type: "prompt", text: "hi" }, { type: "prompt", text: "hi" }],
      [{ type: "followup", text: "more" }, { type: "followup", text: "more" }],
      [{ type: "approve" }, { type: "approve" }],
      [{ type: "reject" }, { type: "reject", reason: undefined }],
      [{ type: "reject", reason: "no" }, { type: "reject", reason: "no" }],
      [{ type: "cancel" }, { type: "cancel" }],
    ];
    for (const [input, expected] of cases) {
      expect(parseCommand(input)).toEqual(expected);
    }
  });

  it("accepts an init carrying a well-formed bridge", () => {
    const input = {
      type: "init",
      config: { bridge: { baseUrl: "http://127.0.0.1:8731", kernelId: "k-123" } },
    };
    expect(parseCommand(input)).toEqual({
      type: "init",
      config: { bridge: { baseUrl: "http://127.0.0.1:8731", kernelId: "k-123" } },
    });
  });
});

describe("parseCommand — malformed bridge is rejected", () => {
  const badBridges: unknown[] = [
    { type: "init", config: { bridge: 5 } }, // not an object
    { type: "init", config: { bridge: null } }, // null object
    { type: "init", config: { bridge: {} } }, // missing both fields
    { type: "init", config: { bridge: { baseUrl: "http://x" } } }, // missing kernelId
    { type: "init", config: { bridge: { kernelId: "k" } } }, // missing baseUrl
    { type: "init", config: { bridge: { baseUrl: 1, kernelId: "k" } } }, // wrong baseUrl type
    { type: "init", config: { bridge: { baseUrl: "http://x", kernelId: 2 } } }, // wrong kernelId type
    { type: "init", config: { bridge: { baseUrl: "", kernelId: "k" } } }, // empty baseUrl
    { type: "init", config: { bridge: { baseUrl: "http://x", kernelId: "" } } }, // empty kernelId
  ];

  it("returns null for every malformed bridge without throwing", () => {
    for (const value of badBridges) {
      expect(() => parseCommand(value)).not.toThrow();
      expect(parseCommand(value), `should reject: ${JSON.stringify(value)}`).toBeNull();
    }
  });
});

describe("parseCommand — malformed input is rejected, never thrown", () => {
  const malformed: unknown[] = [
    null,
    undefined,
    42,
    "init",
    [],
    {},
    { type: 123 },
    { type: "nope" },
    { type: "prompt" }, // missing text
    { type: "prompt", text: 7 }, // wrong text type
    { type: "followup" }, // missing text
    { type: "followup", text: null },
    { type: "init", config: "bad" }, // config must be object|undefined
    { type: "init", config: 5 },
    { type: "reject", reason: 9 }, // reason must be string|undefined
  ];

  it("returns null for every malformed value without throwing", () => {
    for (const value of malformed) {
      expect(() => parseCommand(value)).not.toThrow();
      expect(parseCommand(value), `should reject: ${JSON.stringify(value)}`).toBeNull();
    }
  });
});

describe("encodeOutbound", () => {
  it("emits exactly one newline-terminated JSON object", () => {
    const line = encodeOutbound({ type: "exit", code: 0 });
    expect(line.endsWith("\n")).toBe(true);
    expect(line.indexOf("\n")).toBe(line.length - 1);
    expect(JSON.parse(line)).toEqual({ type: "exit", code: 0 });
  });

  it("round-trips a ready frame", () => {
    const line = encodeOutbound({
      type: "ready",
      protocolVersion: PROTOCOL_VERSION,
      piVersion: "0.0.0",
      model: null,
      tools: { activeToolNames: [], customToolCount: 0, noTools: "all" },
    });
    expect(JSON.parse(line).type).toBe("ready");
  });
});
