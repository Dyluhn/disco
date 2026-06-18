/**
 * DeckResolver — §4.3 C-EDIT-3 (deck half).
 *
 * Reads `data-element-id` + `data-slide-id` attributes stamped by the C3
 * HTML renderer (`_pptx_render.py render_html`) and resolves them to:
 *   - A `DeckRef` (for the SelectionEnvelope / useElementSelect bridge).
 *   - A RFC-6901 JSON Pointer into the `AuthoredDeck` document
 *     (for `deck_patch` tool arguments).
 *
 * Element ID encoding (mirrors `_deck_schema.py lower_deck()`):
 *   `"slide-{i}:title"`        → `/slides/{i}/title`
 *   `"slide-{i}:subtitle"`     → `/slides/{i}/body/0`   (subtitle = body[0] on title/section slides)
 *   `"slide-{i}:body:{j}"`     → `/slides/{i}/body/{j}`
 *   `"slide-{i}:chart"`        → `/slides/{i}/chart`
 *   `"slide-{i}:table"`        → `/slides/{i}/table`
 *   `"slide-{i}:image_prompt"` → `/slides/{i}/image_prompt`
 *   `"slide-{i}:notes"`        → `/slides/{i}/notes`
 *
 * @module deckResolver
 */

import type { DeckRef } from "@/lib/selectionBridge";

// ─── Public interface ────────────────────────────────────────────────────────

/**
 * Walk up from `el` to find the nearest element tagged with
 * `data-element-id` + `data-slide-id`.
 *
 * Returns a `DeckRef` when found, or `null` when no tagged ancestor exists
 * within the document element (i.e. the element is not inside a rendered deck).
 */
export function resolveFromTag(el: Element): DeckRef | null {
  let cur: Element | null = el;
  while (cur && cur !== document.documentElement) {
    const elementId = cur.getAttribute("data-element-id");
    const slideId = cur.getAttribute("data-slide-id");
    if (elementId && slideId) {
      return { kind: "deck", slide_id: slideId, element_id: elementId };
    }
    cur = cur.parentElement;
  }
  return null;
}

/**
 * Convert a `DeckRef.element_id` (e.g. `"slide-2:body:1"`) into an
 * RFC-6901 JSON Pointer (e.g. `"/slides/2/body/1"`).
 *
 * Returns `null` for element IDs that do not match the known encoding.
 * The pointer can be used directly in a `deck_patch` tool call.
 */
export function elementIdToJsonPath(elementId: string): string | null {
  // Expected shape: "slide-{i}:{field}[:{j}]"
  const prefix = "slide-";
  if (!elementId.startsWith(prefix)) return null;

  const rest = elementId.slice(prefix.length); // e.g. "2:body:1"
  const colonIdx = rest.indexOf(":");
  if (colonIdx === -1) return null;

  const slideIdx = rest.slice(0, colonIdx); // "2"
  const fieldPart = rest.slice(colonIdx + 1); // "body:1" | "title" | "chart" | etc.

  // Validate slide index is a non-negative integer
  if (!/^\d+$/.test(slideIdx)) return null;
  const i = parseInt(slideIdx, 10);

  // Field dispatch
  if (fieldPart === "title") return `/slides/${i}/title`;
  if (fieldPart === "subtitle") return `/slides/${i}/body/0`;
  if (fieldPart === "chart") return `/slides/${i}/chart`;
  if (fieldPart === "table") return `/slides/${i}/table`;
  if (fieldPart === "image_prompt") return `/slides/${i}/image_prompt`;
  if (fieldPart === "notes") return `/slides/${i}/notes`;

  // "body:{j}" — a bullet at index j
  if (fieldPart.startsWith("body:")) {
    const jStr = fieldPart.slice("body:".length);
    if (!/^\d+$/.test(jStr)) return null;
    return `/slides/${i}/body/${parseInt(jStr, 10)}`;
  }

  return null;
}

/**
 * Parse the slide index from a `slide_id` attribute (e.g. `"slide-3"` → `3`).
 * Returns `null` when the format is unexpected.
 */
export function slideIdToIndex(slideId: string): number | null {
  if (!slideId.startsWith("slide-")) return null;
  const rest = slideId.slice("slide-".length);
  if (!/^\d+$/.test(rest)) return null;
  return parseInt(rest, 10);
}

/**
 * Build a human-readable label for a `DeckRef` from the element's text
 * content (passed in from the caller — the resolver does not touch the DOM
 * at label-time, only at resolve-time).
 *
 * If `textContent` is blank, falls back to the element kind parsed from
 * the `element_id`.
 */
export function makeHumanLabel(ref: DeckRef, textContent: string): string {
  const trimmed = textContent.trim().replace(/\s+/g, " ").slice(0, 60);
  const kind = ref.element_id.split(":").slice(1).join(":"); // e.g. "body:1"
  return trimmed ? `${kind} — "${trimmed}"` : kind;
}
