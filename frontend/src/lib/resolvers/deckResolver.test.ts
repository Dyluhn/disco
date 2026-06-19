/**
 * DeckResolver — unit tests (§4.3).
 *
 * Covers:
 *  1. resolveFromTag: exact hit — element has data-element-id + data-slide-id.
 *  2. resolveFromTag: ancestor walk-up — child of a tagged parent.
 *  3. resolveFromTag: no tag anywhere → returns null.
 *  4. resolveFromTag: uses the nearest (innermost) ancestor's tag.
 *  5. elementIdToJsonPath: title → /slides/{i}/title.
 *  6. elementIdToJsonPath: subtitle → /slides/{i}/body/0.
 *  7. elementIdToJsonPath: body:{j} → /slides/{i}/body/{j}.
 *  8. elementIdToJsonPath: chart / table / image_prompt / notes.
 *  9. elementIdToJsonPath: unknown field → null.
 * 10. elementIdToJsonPath: malformed element_id → null.
 * 11. Round-trip: resolveFromTag → elementIdToJsonPath.
 * 12. slideIdToIndex: parses slide index correctly.
 * 13. slideIdToIndex: malformed → null.
 * 14. makeHumanLabel: with text content.
 * 15. makeHumanLabel: blank text → falls back to kind.
 */

import { describe, expect, it } from "vitest";
import {
  elementIdToJsonPath,
  makeHumanLabel,
  resolveFromTag,
  slideIdToIndex,
} from "@/lib/resolvers/deckResolver";
import type { DeckRef } from "@/lib/selectionBridge";

// ─── DOM helpers ─────────────────────────────────────────────────────────────

function taggedEl(elementId: string, slideId: string, textContent = ""): Element {
  const el = document.createElement("div");
  el.setAttribute("data-element-id", elementId);
  el.setAttribute("data-slide-id", slideId);
  el.textContent = textContent;
  return el;
}

function childOf(parent: Element, tagName = "span"): Element {
  const child = document.createElement(tagName);
  parent.appendChild(child);
  return child;
}

// ─── resolveFromTag ───────────────────────────────────────────────────────────

describe("resolveFromTag", () => {
  it("returns DeckRef for an element with exact data-element-id + data-slide-id", () => {
    const el = taggedEl("slide-0:title", "slide-0");
    const ref = resolveFromTag(el);
    expect(ref).not.toBeNull();
    expect(ref!.kind).toBe("deck");
    expect(ref!.slide_id).toBe("slide-0");
    expect(ref!.element_id).toBe("slide-0:title");
  });

  it("walks up to find the nearest ancestor with data-element-id", () => {
    const parent = taggedEl("slide-1:body:2", "slide-1");
    const child = childOf(parent, "em");
    const ref = resolveFromTag(child);
    expect(ref).not.toBeNull();
    expect(ref!.element_id).toBe("slide-1:body:2");
    expect(ref!.slide_id).toBe("slide-1");
  });

  it("returns null when no ancestor is tagged", () => {
    const untagged = document.createElement("div");
    const child = childOf(untagged, "span");
    expect(resolveFromTag(child)).toBeNull();
  });

  it("returns the innermost tagged ancestor (not the outer one)", () => {
    const outer = taggedEl("slide-0:body:0", "slide-0");
    const inner = taggedEl("slide-0:body:1", "slide-0");
    outer.appendChild(inner);
    const grandchild = childOf(inner);

    const ref = resolveFromTag(grandchild);
    expect(ref!.element_id).toBe("slide-0:body:1"); // innermost wins
  });
});

// ─── elementIdToJsonPath ──────────────────────────────────────────────────────

describe("elementIdToJsonPath", () => {
  it("title → /slides/{i}/title", () => {
    expect(elementIdToJsonPath("slide-0:title")).toBe("/slides/0/title");
    expect(elementIdToJsonPath("slide-3:title")).toBe("/slides/3/title");
  });

  it("subtitle → /slides/{i}/body/0", () => {
    expect(elementIdToJsonPath("slide-0:subtitle")).toBe("/slides/0/body/0");
    expect(elementIdToJsonPath("slide-5:subtitle")).toBe("/slides/5/body/0");
  });

  it("body:{j} → /slides/{i}/body/{j}", () => {
    expect(elementIdToJsonPath("slide-0:body:0")).toBe("/slides/0/body/0");
    expect(elementIdToJsonPath("slide-2:body:3")).toBe("/slides/2/body/3");
    expect(elementIdToJsonPath("slide-10:body:12")).toBe("/slides/10/body/12");
  });

  it("chart → /slides/{i}/chart", () => {
    expect(elementIdToJsonPath("slide-1:chart")).toBe("/slides/1/chart");
  });

  it("table → /slides/{i}/table", () => {
    expect(elementIdToJsonPath("slide-4:table")).toBe("/slides/4/table");
  });

  it("image_prompt → /slides/{i}/image_prompt", () => {
    expect(elementIdToJsonPath("slide-0:image_prompt")).toBe("/slides/0/image_prompt");
  });

  it("notes → /slides/{i}/notes", () => {
    expect(elementIdToJsonPath("slide-2:notes")).toBe("/slides/2/notes");
  });

  it("unknown field returns null", () => {
    expect(elementIdToJsonPath("slide-0:foobar")).toBeNull();
  });

  it("malformed (no 'slide-' prefix) returns null", () => {
    expect(elementIdToJsonPath("other-0:title")).toBeNull();
  });

  it("malformed (no colon separator) returns null", () => {
    expect(elementIdToJsonPath("slide-0title")).toBeNull();
  });

  it("non-integer slide index returns null", () => {
    expect(elementIdToJsonPath("slide-abc:title")).toBeNull();
  });

  it("non-integer body index returns null", () => {
    expect(elementIdToJsonPath("slide-0:body:x")).toBeNull();
  });
});

// ─── Round-trip ───────────────────────────────────────────────────────────────

describe("resolveFromTag + elementIdToJsonPath round-trip", () => {
  it("resolves a tagged element to a DeckRef then converts to a JSON Pointer", () => {
    const el = taggedEl("slide-2:body:1", "slide-2", "Some bullet text");
    const ref = resolveFromTag(el);
    expect(ref).not.toBeNull();

    const pointer = elementIdToJsonPath(ref!.element_id);
    expect(pointer).toBe("/slides/2/body/1");
  });

  it("resolves a title element to /slides/{i}/title", () => {
    const el = taggedEl("slide-4:title", "slide-4", "My Title");
    const ref = resolveFromTag(el);
    const pointer = elementIdToJsonPath(ref!.element_id);
    expect(pointer).toBe("/slides/4/title");
  });
});

// ─── slideIdToIndex ───────────────────────────────────────────────────────────

describe("slideIdToIndex", () => {
  it("parses 'slide-0' → 0", () => {
    expect(slideIdToIndex("slide-0")).toBe(0);
  });

  it("parses 'slide-12' → 12", () => {
    expect(slideIdToIndex("slide-12")).toBe(12);
  });

  it("returns null for malformed input", () => {
    expect(slideIdToIndex("other-0")).toBeNull();
    expect(slideIdToIndex("slide-abc")).toBeNull();
    expect(slideIdToIndex("")).toBeNull();
  });
});

// ─── makeHumanLabel ───────────────────────────────────────────────────────────

describe("makeHumanLabel", () => {
  const ref: DeckRef = {
    kind: "deck",
    slide_id: "slide-0",
    element_id: "slide-0:title",
  };

  it("includes trimmed text content in label", () => {
    const label = makeHumanLabel(ref, "  My Title Slide  ");
    expect(label).toContain("My Title Slide");
    expect(label).toContain("title");
  });

  it("falls back to kind when text is blank", () => {
    const label = makeHumanLabel(ref, "   ");
    expect(label).toBe("title");
  });

  it("truncates long text at 60 chars", () => {
    const long = "A".repeat(100);
    const label = makeHumanLabel(ref, long);
    expect(label.length).toBeLessThan(100);
    expect(label).toContain("A".repeat(60));
  });

  it("collapses whitespace in text", () => {
    const label = makeHumanLabel(ref, "  Hello   World  ");
    expect(label).toContain("Hello World");
    expect(label).not.toContain("  ");
  });
});
