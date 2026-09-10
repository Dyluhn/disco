/**
 * AppResolver — A1.2 (the website/app half of click-to-edit).
 *
 * The server-side stamper (`oid_stamp.py stamp_oids`) writes
 * `data-oid="{workspace-relpath}:{sourceline}"` onto every visible element of a
 * built static HTML page. The in-frame selection agent (`selectionAgent.ts
 * resolveRef`) parses that attribute into a `SourceRef{kind:'source', oid, file,
 * line}` and ships it in the SelectionEnvelope.
 *
 * This module is the host-side counterpart of `deckResolver.ts`: pure helpers
 * that turn a raw `data-oid` string into a `{file, line}` (for forming the edit
 * steer instruction) and a human label (for the edit affordance header). It does
 * NOT touch the DOM — the in-frame agent already walked up to the nearest stamped
 * ancestor; here we only parse the resulting oid.
 *
 * @module appResolver
 */

import type { SelectionEnvelope } from "@/lib/selectionBridge";

/** A parsed `data-oid`: the workspace-relative source file + its 1-based line. */
export interface AppRef {
  /** Workspace-relative path of the source HTML file (e.g. "index.html"). */
  file: string;
  /** 1-based source line of the stamped element. */
  line: number;
}

/**
 * Parse a `data-oid` value (`"{file}:{line}"`) into an `AppRef`.
 *
 * Returns `null` when the oid is empty, has no `file` part, or carries no usable
 * positive line number — i.e. it is NOT a real, editable source reference. The
 * caller uses `null` to gate the edit affordance off (no false affordance for a
 * non-stamped / dynamically-inserted element whose oid is blank).
 *
 * The line is the part AFTER the last colon, so a `file` containing a colon
 * (unusual but legal on some filesystems) still parses. A `:0` (the stamper's
 * fallback when lxml had no source line) yields `null` — there is no real line to
 * steer toward.
 */
export function parseOid(oid: string): AppRef | null {
  if (!oid) return null;
  const sep = oid.lastIndexOf(":");
  if (sep <= 0) return null; // no colon, or leading colon (empty file)
  const file = oid.slice(0, sep);
  const lineStr = oid.slice(sep + 1);
  if (!file) return null;
  if (!/^\d+$/.test(lineStr)) return null;
  const line = parseInt(lineStr, 10);
  if (line <= 0) return null;
  return { file, line };
}

/**
 * Build a human-readable label for the edit affordance from an `AppRef` and the
 * element's text content (passed in by the caller — the resolver does not read
 * the DOM). Mirrors deckResolver.makeHumanLabel's shape.
 */
export function humanLabel(ref: AppRef, textContent: string): string {
  const trimmed = textContent.trim().replace(/\s+/g, " ").slice(0, 60);
  const loc = `${ref.file}:${ref.line}`;
  return trimmed ? `${loc} — “${trimmed}”` : loc;
}

/**
 * W-26 — turn ANY selection envelope (source OR deck OR non-source) into a steer
 * string that names the element, for the "Discuss with agent" affordance. It carries
 * no instruction — it just hands the agent the context of WHAT the user clicked so a
 * conversation can begin. It lands via the existing steer → send_message path. (The
 * scoped "Change this element" path is separate — it goes through the `selection_edit`
 * wire so the HOST builds a precisely-anchored targeted-edit directive.)
 *
 * - source: `{file}:{line}` plus the human label (de-duped if the label already
 *   leads with the location, which `humanLabel` produces).
 * - deck:   `slide {slide_id} · element {element_id}` plus the human label.
 */
export function formatSelectionContext(sel: SelectionEnvelope): string {
  const ref = sel.selection_ref;
  const label = sel.human_label?.trim() ?? "";
  let target: string;
  if (ref.kind === "source") {
    const loc = `${ref.file}:${ref.line}`;
    // humanLabel() already prefixes the location for source refs — don't repeat it.
    target = label ? (label.startsWith(loc) ? label : `${label} (${loc})`) : loc;
  } else if (ref.kind === "deck") {
    const loc = `slide ${ref.slide_id} · element ${ref.element_id}`;
    target = label ? `${label} (${loc})` : loc;
  } else {
    // semantic (data-disco-*) ref — describe by section/field.
    const loc = ref.field_id
      ? `${ref.section_id}.${ref.field_id}`
      : ref.collection_id && ref.index !== undefined
        ? `${ref.collection_id}[${ref.index}]`
        : `${ref.section_id} section`;
    target = label ? `${label} (${loc})` : loc;
  }
  return `Let's discuss the element I selected in the preview: ${target}.`;
}
