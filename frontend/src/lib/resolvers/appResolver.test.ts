/**
 * AppResolver — unit tests (A1.2).
 *
 * Covers:
 *  1. parseOid: a real "file:line" → {file, line}.
 *  2. parseOid: nested path "dir/index.html:42" → {file:"dir/index.html", line:42}.
 *  3. parseOid: empty string → null (no false affordance).
 *  4. parseOid: blank file (":12") → null.
 *  5. parseOid: no colon → null.
 *  6. parseOid: :0 (stamper fallback, no real line) → null.
 *  7. parseOid: non-numeric line → null.
 *  8. parseOid: a file containing a colon parses on the LAST colon.
 *  9. humanLabel: with text content.
 * 10. humanLabel: blank text → falls back to file:line.
 */

import { describe, expect, it } from "vitest";
import {
  formatEditSteer,
  formatSelectionContext,
  humanLabel,
  parseOid,
} from "@/lib/resolvers/appResolver";
import type { SelectionEnvelope } from "@/lib/selectionBridge";

const NONCE = "aabbccddeeff00112233445566778899";
function envelope(over: Partial<SelectionEnvelope>): SelectionEnvelope {
  return {
    v: 1,
    channel: "disco-select",
    nonce: NONCE,
    type: "disco:selection",
    selection_ref: { kind: "source", oid: "index.html:42", file: "index.html", line: 42 },
    human_label: "index.html:42 — “Q3 Revenue”",
    rect: { x: 0, y: 0, width: 10, height: 10 },
    ...over,
  };
}

describe("parseOid", () => {
  it("parses a real file:line oid", () => {
    expect(parseOid("index.html:42")).toEqual({ file: "index.html", line: 42 });
  });

  it("parses a nested workspace path", () => {
    expect(parseOid("site/pages/about.html:7")).toEqual({
      file: "site/pages/about.html",
      line: 7,
    });
  });

  it("returns null for an empty oid (non-stamped element)", () => {
    expect(parseOid("")).toBeNull();
  });

  it("returns null when the file part is empty", () => {
    expect(parseOid(":12")).toBeNull();
  });

  it("returns null when there is no colon", () => {
    expect(parseOid("index.html")).toBeNull();
  });

  it("returns null for the :0 stamper fallback (no real source line)", () => {
    expect(parseOid("index.html:0")).toBeNull();
  });

  it("returns null for a non-numeric line", () => {
    expect(parseOid("index.html:abc")).toBeNull();
  });

  it("splits on the LAST colon so a colon-bearing file still parses", () => {
    expect(parseOid("weird:name.html:99")).toEqual({
      file: "weird:name.html",
      line: 99,
    });
  });
});

describe("humanLabel", () => {
  it("includes the trimmed text content", () => {
    expect(humanLabel({ file: "index.html", line: 12 }, "  Hello   world  ")).toBe(
      'index.html:12 — “Hello world”',
    );
  });

  it("falls back to file:line when text is blank", () => {
    expect(humanLabel({ file: "index.html", line: 12 }, "   ")).toBe("index.html:12");
  });
});

describe("formatEditSteer", () => {
  it("forms the In {file} near line {line}, {instruction} steer", () => {
    expect(
      formatEditSteer({ file: "index.html", line: 42 }, "make the heading larger"),
    ).toBe("In index.html near line 42, make the heading larger");
  });

  it("trims the instruction", () => {
    expect(formatEditSteer({ file: "a.html", line: 3 }, "  fix the color  ")).toBe(
      "In a.html near line 3, fix the color",
    );
  });

  it("returns null for a blank instruction (no steer on empty input)", () => {
    expect(formatEditSteer({ file: "a.html", line: 3 }, "   ")).toBeNull();
  });
});

describe("formatSelectionContext (W-26)", () => {
  it("names a source element with its file:line, not duplicating the location", () => {
    // human_label already leads with the location (humanLabel's shape) → no repeat.
    const out = formatSelectionContext(envelope({}));
    expect(out).toBe(
      "Let's discuss the element I selected in the preview: index.html:42 — “Q3 Revenue”.",
    );
  });

  it("appends file:line when the label does NOT already carry it", () => {
    const out = formatSelectionContext(
      envelope({
        selection_ref: { kind: "source", oid: "app.tsx:7", file: "app.tsx", line: 7 },
        human_label: "button.cta — “Submit”",
      }),
    );
    expect(out).toBe(
      "Let's discuss the element I selected in the preview: button.cta — “Submit” (app.tsx:7).",
    );
  });

  it("names a deck element by slide + element ref", () => {
    const out = formatSelectionContext(
      envelope({
        selection_ref: { kind: "deck", slide_id: "s3", element_id: "title" },
        human_label: "h2.title — “Revenue”",
      }),
    );
    expect(out).toBe(
      "Let's discuss the element I selected in the preview: h2.title — “Revenue” (slide s3 · element title).",
    );
  });

  it("falls back to the bare ref when there is no human label", () => {
    const out = formatSelectionContext(
      envelope({
        selection_ref: { kind: "deck", slide_id: "s1", element_id: "img" },
        human_label: "",
      }),
    );
    expect(out).toBe(
      "Let's discuss the element I selected in the preview: slide s1 · element img.",
    );
  });
});
