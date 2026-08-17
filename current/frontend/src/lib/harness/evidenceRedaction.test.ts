import { describe, expect, it } from "vitest";

import {
  redactFailureStringsInPlace,
  redactKnownIdentifiers,
  redactKnownIdentifiersInPlace,
} from "./evidenceRedaction";

describe("evidence redaction", () => {
  it("preserves primary Error identity while redacting nested failure evidence", () => {
    const identifier = "conv_12345678sensitive";
    const primary = Object.assign(new Error(`request for ${identifier} failed`), {
      matcherResult: {
        actual: `http://example.invalid/previews/${identifier}/`,
      },
      cause: new Error("prefix 12345678 failed"),
    });

    const redacted = redactKnownIdentifiersInPlace(primary, [identifier]);

    expect(redacted).toBe(primary);
    expect(primary.message).not.toContain(identifier);
    expect(primary.stack).not.toContain(identifier);
    expect(JSON.stringify(primary.matcherResult)).not.toContain(identifier);
    expect(primary.cause.message).not.toContain("12345678");
  });

  it("redacts full and compact identifiers from retained strings", () => {
    const identifier = "conv_abcdef12remaining";
    expect(redactKnownIdentifiers(`${identifier} abcdef12`, [identifier])).toBe(
      "[REDACTED_CONVERSATION_ID] [REDACTED_CID_PREFIX]",
    );
  });

  it("fails closed for frozen errors and native non-enumerable causes", () => {
    const identifier = "conv_fedcba98sensitive";
    const cause = Object.freeze(new Error("cause fedcba98"));
    const primary = Object.freeze(
      new Error(`request ${identifier} failed`, {
        cause,
      }),
    );

    const redacted = redactKnownIdentifiersInPlace(primary, [identifier]);

    expect(redacted).toBeInstanceOf(Error);
    expect(redacted).not.toBe(primary);
    const retained = redacted as Error & { cause?: Error };
    expect(retained.message).not.toContain(identifier);
    expect(retained.stack).not.toContain(identifier);
    expect(retained.cause?.message).not.toContain("fedcba98");
  });

  it("replaces over-depth failure objects instead of retaining raw identifiers", () => {
    const identifier = "conv_2468ace0sensitive";
    const root: Record<string, unknown> = {};
    let cursor = root;
    for (let depth = 0; depth < 12; depth += 1) {
      const next: Record<string, unknown> = {};
      cursor.next = next;
      cursor = next;
    }
    cursor.secret = identifier;
    const primary = new Error("outer failure", { cause: root });

    const redacted = redactKnownIdentifiersInPlace(primary, [identifier]) as Error & {
      cause?: unknown;
    };

    expect(JSON.stringify(redacted.cause)).not.toContain(identifier);
    expect(JSON.stringify(redacted.cause)).toContain("REDACTED_DEEP_FAILURE_EVIDENCE");
  });

  it("applies a caller-supplied redactor to messages, stacks, causes, and matcher fields", () => {
    const secret = "one-time-preview-secret";
    const primary = Object.assign(new Error(`request intent=${secret} failed`), {
      matcherResult: { actual: `https://preview.invalid/?intent=${secret}` },
      cause: new Error(`nested ${secret}`),
    });

    const retained = redactFailureStringsInPlace(primary, (value) =>
      value.replaceAll(secret, "[REDACTED_PREVIEW_BEARER]"),
    ) as typeof primary;

    expect(retained).toBe(primary);
    expect(retained.message).not.toContain(secret);
    expect(retained.stack).not.toContain(secret);
    expect(JSON.stringify(retained.matcherResult)).not.toContain(secret);
    expect(retained.cause.message).not.toContain(secret);
  });
});
