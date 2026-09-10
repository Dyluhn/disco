/**
 * selectionBridge — unit tests for the postMessage bridge protocol.
 *
 * Covers:
 *  1. generateNonce: uniqueness + hex format.
 *  2. parseInFrameMessage: valid envelope round-trips.
 *  3. Security guards: wrong origin / wrong nonce / bad version / bad channel.
 *  4. Command builders: makeArmCommand etc. produce correct shapes.
 */

import { describe, expect, it } from "vitest";
import {
  generateNonce,
  parseInFrameMessage,
  makeArmCommand,
  makeDisarmCommand,
  makeWalkUpCommand,
} from "@/lib/selectionBridge";
import type { SelectionEnvelope } from "@/lib/selectionBridge";

// ─── Helper: build a synthetic MessageEvent ──────────────────────────────────

function makeEvent(origin: string, data: unknown): MessageEvent {
  return new MessageEvent("message", { origin, data });
}

// ─── Baseline valid envelope ──────────────────────────────────────────────────

const VALID_NONCE = "deadbeef0102030405060708090a0b0c";
const ALLOWED_ORIGIN = "null"; // srcdoc sandboxed frame

const validPayload: SelectionEnvelope = {
  v: 1,
  channel: "disco-select",
  nonce: VALID_NONCE,
  type: "disco:selection",
  selection_ref: { kind: "source", oid: "src/App.tsx:12:4", file: "src/App.tsx", line: 12 },
  human_label: "button.primary — \"Submit\"",
  rect: { x: 10, y: 20, width: 100, height: 40 },
};

// ─── generateNonce ────────────────────────────────────────────────────────────

describe("generateNonce", () => {
  it("produces a 32-char hex string", () => {
    const n = generateNonce();
    expect(n).toMatch(/^[0-9a-f]{32}$/);
  });

  it("produces unique values on successive calls", () => {
    const nonces = new Set(Array.from({ length: 20 }, () => generateNonce()));
    expect(nonces.size).toBe(20);
  });
});

// ─── Valid envelope round-trip ────────────────────────────────────────────────

describe("parseInFrameMessage — valid envelope", () => {
  it("returns the SelectionEnvelope for a well-formed disco:selection", () => {
    const event = makeEvent(ALLOWED_ORIGIN, validPayload);
    const result = parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE);
    expect(result).not.toBeNull();
    expect(result?.type).toBe("disco:selection");
    if (result?.type === "disco:selection") {
      expect(result.human_label).toBe(validPayload.human_label);
      expect(result.rect).toEqual(validPayload.rect);
      expect(result.selection_ref).toEqual(validPayload.selection_ref);
    }
  });

  it("returns a HoverEnvelope for a well-formed disco:hover", () => {
    const event = makeEvent(ALLOWED_ORIGIN, {
      v: 1,
      channel: "disco-select",
      nonce: VALID_NONCE,
      type: "disco:hover",
      rect: { x: 5, y: 5, width: 50, height: 20 },
    });
    const result = parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE);
    expect(result).not.toBeNull();
    expect(result?.type).toBe("disco:hover");
  });

  it("accepts a DeckRef selection_ref", () => {
    const deckPayload = {
      ...validPayload,
      selection_ref: { kind: "deck", slide_id: "slide-1", element_id: "el-42" },
    };
    const event = makeEvent(ALLOWED_ORIGIN, deckPayload);
    const result = parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE);
    expect(result).not.toBeNull();
    if (result?.type === "disco:selection") {
      expect(result.selection_ref.kind).toBe("deck");
    }
  });

  it("passes through optional screenshot_crop", () => {
    const withCrop = { ...validPayload, screenshot_crop: "data:image/png;base64,abc" };
    const result = parseInFrameMessage(
      makeEvent(ALLOWED_ORIGIN, withCrop),
      ALLOWED_ORIGIN,
      VALID_NONCE,
    );
    expect(result).not.toBeNull();
    if (result?.type === "disco:selection") {
      expect(result.screenshot_crop).toBe("data:image/png;base64,abc");
    }
  });
});

// ─── Security: bad origin ─────────────────────────────────────────────────────

describe("parseInFrameMessage — forged origin (MUST be rejected)", () => {
  it("rejects a message from a different origin", () => {
    const event = makeEvent("https://evil.example.com", validPayload);
    const result = parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE);
    expect(result).toBeNull();
  });

  it("rejects when allowed origin is a specific URL but event.origin differs", () => {
    const proxyOrigin = "http://p2-abc12345-8000.localhost:8000";
    const event = makeEvent("http://other-host:9000", validPayload);
    const result = parseInFrameMessage(event, proxyOrigin, VALID_NONCE);
    expect(result).toBeNull();
  });

  it('accepts "null" origin for srcdoc sandboxed frames', () => {
    const event = makeEvent("null", validPayload);
    const result = parseInFrameMessage(event, "null", VALID_NONCE);
    expect(result).not.toBeNull();
  });
});

// ─── Security: bad nonce ──────────────────────────────────────────────────────

describe("parseInFrameMessage — forged nonce (MUST be rejected)", () => {
  it("rejects a message with a different nonce", () => {
    const wrongNonce = generateNonce();
    const payload = { ...validPayload, nonce: wrongNonce };
    const event = makeEvent(ALLOWED_ORIGIN, payload);
    const result = parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE);
    expect(result).toBeNull();
  });

  it("rejects a message with an empty nonce", () => {
    const payload = { ...validPayload, nonce: "" };
    const event = makeEvent(ALLOWED_ORIGIN, payload);
    const result = parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE);
    expect(result).toBeNull();
  });

  it("rejects when the session nonce is empty (not armed)", () => {
    const event = makeEvent(ALLOWED_ORIGIN, validPayload);
    const result = parseInFrameMessage(event, ALLOWED_ORIGIN, "");
    expect(result).toBeNull();
  });
});

// ─── Security: malformed frames ───────────────────────────────────────────────

describe("parseInFrameMessage — malformed frames", () => {
  it("rejects null data", () => {
    const event = makeEvent(ALLOWED_ORIGIN, null);
    expect(parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE)).toBeNull();
  });

  it("rejects wrong v (version)", () => {
    const payload = { ...validPayload, v: 2 };
    const event = makeEvent(ALLOWED_ORIGIN, payload);
    expect(parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE)).toBeNull();
  });

  it("rejects wrong channel", () => {
    const payload = { ...validPayload, channel: "other-channel" };
    const event = makeEvent(ALLOWED_ORIGIN, payload);
    expect(parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE)).toBeNull();
  });

  it("rejects unknown type", () => {
    const payload = { ...validPayload, type: "disco:unknown" };
    const event = makeEvent(ALLOWED_ORIGIN, payload);
    expect(parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE)).toBeNull();
  });

  it("rejects disco:selection with missing rect", () => {
    const { rect: _unused, ...noRect } = validPayload;
    void _unused;
    const event = makeEvent(ALLOWED_ORIGIN, noRect);
    expect(parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE)).toBeNull();
  });

  it("rejects disco:selection with non-numeric rect fields", () => {
    const payload = { ...validPayload, rect: { x: "10", y: 20, width: 100, height: 40 } };
    const event = makeEvent(ALLOWED_ORIGIN, payload);
    expect(parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE)).toBeNull();
  });

  it("rejects disco:selection with missing selection_ref", () => {
    const { selection_ref: _unused, ...noRef } = validPayload;
    void _unused;
    const event = makeEvent(ALLOWED_ORIGIN, noRef);
    expect(parseInFrameMessage(event, ALLOWED_ORIGIN, VALID_NONCE)).toBeNull();
  });
});

// ─── Command builders ─────────────────────────────────────────────────────────

describe("command builders", () => {
  it("makeArmCommand produces correct shape", () => {
    const cmd = makeArmCommand("abc");
    expect(cmd).toEqual({ v: 1, channel: "disco-select", nonce: "abc", type: "disco:overlay:arm" });
  });

  it("makeDisarmCommand produces correct shape", () => {
    const cmd = makeDisarmCommand("abc");
    expect(cmd).toEqual({ v: 1, channel: "disco-select", nonce: "abc", type: "disco:overlay:disarm" });
  });

  it("makeWalkUpCommand produces correct shape", () => {
    const cmd = makeWalkUpCommand("abc");
    expect(cmd).toEqual({ v: 1, channel: "disco-select", nonce: "abc", type: "disco:overlay:walkup" });
  });
});
