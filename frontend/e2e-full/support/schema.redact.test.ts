/**
 * Unit tests for redact() in schema.ts — string-content scrubber coverage.
 *
 * These run under vitest (not Playwright).  They verify the two key properties
 * added by the P1 secret-leak fix:
 *   1. Secret-looking substrings embedded in string values are scrubbed even
 *      when the containing key is not itself sensitive.
 *   2. Neutral long hex strings (SHA / UUID) in plain value position survive
 *      unmodified so the harness doesn't corrupt artifact ids.
 *
 * evidence-harness-campaign.md W4 / P1 fix
 */

import { describe, expect, it } from "vitest";
import { redact } from "./schema";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const REDACTED = "***REDACTED***";

// ---------------------------------------------------------------------------
// Key-based redaction (existing behaviour — must not regress)
// ---------------------------------------------------------------------------

describe("key-based redaction", () => {
  it("replaces values under sensitive keys", () => {
    const result = redact({ api_key: "super-secret", apiKey: "also-secret" });
    expect(result).toEqual({
      api_key: REDACTED,
      apiKey: REDACTED,
    });
  });

  it("replaces nested sensitive keys", () => {
    const result = redact({ config: { token: "tok_abc", name: "disco" } });
    expect(result).toEqual({ config: { token: REDACTED, name: "disco" } });
  });

  it("traverses arrays", () => {
    const result = redact([{ password: "p@ss" }, { label: "ok" }]);
    expect(result).toEqual([{ password: REDACTED }, { label: "ok" }]);
  });
});

// ---------------------------------------------------------------------------
// String-content scrubber (new behaviour)
// ---------------------------------------------------------------------------

describe("string-content scrubber — rule 1: sk- prefixed keys", () => {
  it("scrubs sk- token embedded in a non-sensitive value", () => {
    // The key 'content' is not sensitive; the secret is inside the string.
    const result = redact({ content: "key sk-ABCDEFGHIJKLMNOPQRSTUVWX" });
    expect(result).toEqual({ content: `key ${REDACTED}` });
  });

  it("scrubs sk-proj- variant", () => {
    const result = redact({
      msg: "using sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
    });
    expect((result as Record<string, string>).msg).toContain(REDACTED);
    expect((result as Record<string, string>).msg).not.toContain("sk-proj-");
  });

  it("leaves a short sk- string alone (under 20 chars after prefix)", () => {
    // "sk-short" = 5 chars — not a real API key, must not be redacted.
    const result = redact({ note: "see sk-short for details" });
    expect(result).toEqual({ note: "see sk-short for details" });
  });
});

describe("string-content scrubber — rule 2: AWS AKIA keys", () => {
  it("scrubs a standalone AKIA key", () => {
    const result = redact({ stdout: "AKIAIOSFODNN7EXAMPLE" });
    expect((result as Record<string, string>).stdout).toBe(REDACTED);
  });

  it("scrubs AKIA key embedded in text", () => {
    const result = redact({ log: "using key AKIAIOSFODNN7EXAMPLE here" });
    expect((result as Record<string, string>).log).toContain(REDACTED);
    expect((result as Record<string, string>).log).not.toContain("AKIA");
  });
});

describe("string-content scrubber — rule 3: Bearer tokens", () => {
  it("scrubs Bearer token in Authorization header value", () => {
    const result = redact({
      header: "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc",
    });
    expect((result as Record<string, string>).header).toBe(REDACTED);
  });

  it("is case-insensitive for Bearer", () => {
    const result = redact({ auth: "bearer ABCDEFGHIJKLMNOPQRSTUVWXYZ" });
    expect((result as Record<string, string>).auth).toBe(REDACTED);
  });
});

describe("string-content scrubber — rule 5: contextual keyword blobs", () => {
  it("scrubs value after api_key= marker, keeps marker", () => {
    const result = redact({ body: "api_key=ABCDEFGHIJKLMNOPQRSTUVWXYZ" });
    expect((result as Record<string, string>).body).toBe(
      `api_key=${REDACTED}`,
    );
  });

  it("scrubs value after token: marker, keeps marker", () => {
    const result = redact({ text: "token: ABCDEFGHIJKLMNOPQRSTUVWXYZ1234" });
    expect((result as Record<string, string>).text).toBe(
      `token: ${REDACTED}`,
    );
  });
});

// ---------------------------------------------------------------------------
// SHA / UUID survival — must NOT be redacted in plain value position
// ---------------------------------------------------------------------------

describe("SHA / UUID survival", () => {
  it("leaves a 40-char git SHA intact under a neutral key", () => {
    // This is the exact case from the task brief.
    const sha = "3f786850e387550fdab836ed7e6dc881de23001b";
    const result = redact({ sha });
    expect(result).toEqual({ sha });
  });

  it("leaves a standard UUID intact", () => {
    const uuid = "550e8400-e29b-41d4-a716-446655440000";
    const result = redact({ artifact_id: uuid });
    expect(result).toEqual({ artifact_id: uuid });
  });

  it("leaves a 64-char hex string intact under a neutral key", () => {
    // Long enough to trigger length checks on rules that don't need a prefix.
    // None of the rules fire without a recognised prefix.
    const hash =
      "a".repeat(32) + "b".repeat(32); // 64 lowercase hex-compatible chars
    const result = redact({ digest: hash });
    expect(result).toEqual({ digest: hash });
  });
});

// ---------------------------------------------------------------------------
// Deeply nested / mixed structures
// ---------------------------------------------------------------------------

describe("nested structures with embedded secrets", () => {
  it("scrubs embedded sk- token inside nested array element", () => {
    const result = redact({
      events: [{ text: "sk-ABCDEFGHIJKLMNOPQRSTUVWX done" }],
    });
    expect(
      (result as { events: Array<{ text: string }> }).events[0]?.text,
    ).toBe(`${REDACTED} done`);
  });

  it("key-based redaction still wins over content scrub for sensitive keys", () => {
    // The key 'token' is sensitive, so the whole value is replaced without
    // even running content scrubbing on it.
    const result = redact({ token: "3f786850e387550fdab836ed7e6dc881de23001b" });
    expect(result).toEqual({ token: REDACTED });
  });
});
