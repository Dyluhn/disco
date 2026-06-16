/**
 * Contract test: every backend event kind has an explicit UI disposition.
 *
 * This is the structural guard against the "backend emits an event the UI
 * silently drops" bug class. If someone adds a backend EventKind (or a new
 * surface starts emitting one) without classifying it here, this test goes red.
 * The companion Python test (test_event_kind_frontend_contract.py) asserts
 * KNOWN_EVENT_KINDS equals the backend enum, closing the cross-language loop.
 */
import { describe, it, expect } from "vitest";
import {
  KNOWN_EVENT_KINDS,
  EVENT_DISPOSITION,
  type KnownEventKind,
} from "./eventDisposition";

describe("event disposition contract", () => {
  it("every known backend event kind has a disposition (no silent drops)", () => {
    for (const kind of KNOWN_EVENT_KINDS) {
      const d = EVENT_DISPOSITION[kind as KnownEventKind];
      expect(d, `event kind "${kind}" has no UI disposition — it would be silently dropped`).toBeDefined();
      expect(["rendered", "suppressed"]).toContain(d.disposition);
      expect(d.where.length, `disposition for "${kind}" needs a where/reason`).toBeGreaterThan(0);
    }
  });

  it("has no disposition entries for unknown kinds (the map mirrors the backend)", () => {
    const known = new Set<string>(KNOWN_EVENT_KINDS);
    for (const kind of Object.keys(EVENT_DISPOSITION)) {
      expect(known.has(kind), `disposition map has "${kind}" which is not a known backend kind`).toBe(true);
    }
  });

  it("KNOWN_EVENT_KINDS has no duplicates", () => {
    expect(new Set(KNOWN_EVENT_KINDS).size).toBe(KNOWN_EVENT_KINDS.length);
  });
});
