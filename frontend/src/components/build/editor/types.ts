/**
 * Shared types for the §4.5 deck editor.
 *
 * These mirror the Python `_deck_schema.py` LoweredDeck / LoweredElement
 * types (geometry as 0–100 % of the 16:9 canvas).
 */

/** Position + size as percentage of the 16:9 slide canvas. */
export interface ElementGeometry {
  x: number;  // 0-100
  y: number;  // 0-100
  w: number;  // 0-100
  h: number;  // 0-100
}

/** One positioned element inside a slide. */
export interface LoweredElement {
  element_id: string;    // e.g. "slide-0:title"
  slide_id: string;      // e.g. "slide-0"
  kind: "title" | "subtitle" | "bullet" | "chart" | "table" | "image_prompt" | "notes";
  content: string;
  geometry: ElementGeometry;
  font_size_vw: number;
  font_weight: "normal" | "bold";
  font_style: "normal" | "italic";
  json_pointer: string;  // RFC-6901 path into AuthoredDeck
}

/** One slide with its positioned elements. */
export interface LoweredSlide {
  slide_id: string;
  slide_idx: number;
  layout: string;
  bg_color: string;
  elements: LoweredElement[];
}

/** The full lowered deck (editor data model). */
export interface LoweredDeck {
  title: string;
  slides: LoweredSlide[];
  theme_name: string;
  theme_mode: string;
}

/**
 * One measured overlay spec — derived by querying `[data-element-id]` rects inside
 * the WYSIWYG iframe and joining against the lowered-deck element map for the
 * json_pointer. Positions are in px relative to the iframe container.
 */
export interface OverlaySpec {
  /** e.g. "slide-0:title" — the iframe's data-element-id value */
  element_id: string;
  /** RFC-6901 path into the AuthoredDeck for this element */
  json_pointer: string;
  kind: LoweredElement["kind"];
  /** Current text content (from the LoweredDeck, NOT the iframe DOM) */
  content: string;
  /** Measured position relative to the iframe container (px). Zero-rect before first measurement. */
  rect: { left: number; top: number; width: number; height: number };
}

/** One RFC-6902 JSON Patch operation. */
export interface JsonPatchOp {
  op: "replace" | "add" | "remove" | "test" | "move" | "copy";
  path: string;
  value?: unknown;
  from?: string;
}
