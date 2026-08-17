/**
 * discoSemanticResolver — P8C (the semantic half of click-to-edit).
 *
 * Given a clicked DOM element inside a built preview, resolve the nearest semantic target
 * from the `data-disco-*` metadata the renderer stamps (P8B), mirroring the P8A locator
 * kinds. The resolution is CLOSEST-FIRST: the first ancestor carrying any data-disco
 * semantic attribute wins, by within-node specificity indexed > field > comment_anchor >
 * section > file — so a far `section`/`file` can never mask a closer `field`/`indexed`/
 * `comment_anchor`. A `field` fails closed if it has no enclosing `section`. When no
 * semantic target applies, fall back to the existing `data-oid` SOURCE ref (appResolver).
 *
 * Pure DOM reads — no postMessage, no side effects. The in-frame selection agent + the
 * host bridge wiring (semantic payloads on the disco-select channel) land in P1B-LIVE
 * with real browser evidence.
 *
 * @module discoSemanticResolver
 */

import { parseOid } from "@/lib/resolvers/appResolver";
import type { SourceRef } from "@/lib/selectionBridge";
import { DISCO_ATTR } from "@/lib/discoSemanticAttrs";

/** A semantic target resolved from data-disco-* metadata (mirrors P8A locators; `file` is
 * frontend file-level metadata, not a P8A locator). */
export type DiscoSemanticTarget =
  | { kind: "field"; section: string; field: string }
  | { kind: "section"; section: string }
  | { kind: "indexed"; collection: string; index: number; itemKind?: string }
  | { kind: "comment_anchor"; anchor: string }
  | { kind: "file"; file: string };

/** What the resolver returns: a semantic target, or the data-oid source fallback. */
export type DiscoResolvedTarget = DiscoSemanticTarget | SourceRef;

/** The within-node target kind, by specificity (indexed > field > comment_anchor > section
 * > file), or null when the element carries no semantic attribute. */
function localKind(el: Element): DiscoSemanticTarget["kind"] | null {
  if (el.hasAttribute(DISCO_ATTR.COLLECTION) && el.hasAttribute(DISCO_ATTR.INDEX)) return "indexed";
  if (el.hasAttribute(DISCO_ATTR.FIELD)) return "field";
  if (el.hasAttribute(DISCO_ATTR.COMMENT_ANCHOR)) return "comment_anchor";
  if (el.hasAttribute(DISCO_ATTR.SECTION)) return "section";
  if (el.hasAttribute(DISCO_ATTR.FILE)) return "file";
  return null;
}

/** The id of the nearest enclosing data-disco-section, or null. */
function nearestSection(el: Element | null): string | null {
  for (let n = el; n; n = n.parentElement) {
    const s = n.getAttribute(DISCO_ATTR.SECTION);
    if (s) return s;
  }
  return null;
}

/** The data-oid source fallback: nearest ancestor with a parseable data-oid. */
function sourceFallback(start: Element): SourceRef | null {
  for (let el: Element | null = start; el; el = el.parentElement) {
    const oid = el.getAttribute("data-oid");
    if (oid) {
      const ref = parseOid(oid);
      if (ref) return { kind: "source", oid, file: ref.file, line: ref.line };
    }
  }
  return null;
}

/** An `indexed` target — a non-negative integer index only, otherwise semantic
 * resolution fails closed (null) so the caller falls back to the data-oid source. */
function resolveIndexedTarget(el: Element): DiscoSemanticTarget | null {
  const collection = el.getAttribute(DISCO_ATTR.COLLECTION) ?? "";
  const idxRaw = el.getAttribute(DISCO_ATTR.INDEX) ?? "";
  if (!/^\d+$/.test(idxRaw)) return null;
  const index = Number(idxRaw);
  const itemKind = el.getAttribute(DISCO_ATTR.ITEM_KIND);
  return itemKind
    ? { kind: "indexed", collection, index, itemKind }
    : { kind: "indexed", collection, index };
}

/** A `field` target — fails closed (null) when it has no enclosing section. */
function resolveFieldTarget(el: Element): DiscoSemanticTarget | null {
  const field = el.getAttribute(DISCO_ATTR.FIELD) ?? "";
  const section = nearestSection(el);
  if (!section) return null;
  return { kind: "field", section, field };
}

function resolveCommentAnchorTarget(el: Element): DiscoSemanticTarget {
  return { kind: "comment_anchor", anchor: el.getAttribute(DISCO_ATTR.COMMENT_ANCHOR) ?? "" };
}

function resolveSectionTarget(el: Element): DiscoSemanticTarget {
  return { kind: "section", section: el.getAttribute(DISCO_ATTR.SECTION) ?? "" };
}

function resolveFileTarget(el: Element): DiscoSemanticTarget {
  return { kind: "file", file: el.getAttribute(DISCO_ATTR.FILE) ?? "" };
}

/** Resolve the target at an element whose local kind is already known. A `null`
 * result means "fail closed at this element" — the caller stops climbing and
 * falls back to the data-oid source (never guesses a section-less field or an
 * invalid index). */
function resolveAtElement(el: Element, kind: DiscoSemanticTarget["kind"]): DiscoSemanticTarget | null {
  if (kind === "indexed") return resolveIndexedTarget(el);
  if (kind === "field") return resolveFieldTarget(el);
  if (kind === "comment_anchor") return resolveCommentAnchorTarget(el);
  if (kind === "section") return resolveSectionTarget(el);
  return resolveFileTarget(el);
}

/**
 * Resolve a clicked element to a semantic target (or the data-oid source fallback, or null).
 * Closest-first; never guesses a section-less field or an invalid index.
 */
export function resolveSemanticTarget(start: Element): DiscoResolvedTarget | null {
  for (let el: Element | null = start; el; el = el.parentElement) {
    const k = localKind(el);
    if (!k) continue;
    const resolved = resolveAtElement(el, k);
    if (!resolved) return sourceFallback(start);
    return resolved;
  }
  return sourceFallback(start);
}

/** Narrow a resolved target to the semantic kinds (excludes the source fallback). */
export function isSemanticTarget(t: DiscoResolvedTarget): t is DiscoSemanticTarget {
  return t.kind !== "source";
}

/** A stable id string for a SEMANTIC target — the steer id for an app_update_content-style
 * edit ("hero.headline" / "hero" / "services.cards[1]" / anchor / file). */
export function semanticEditTarget(t: DiscoSemanticTarget): string {
  switch (t.kind) {
    case "field":
      return `${t.section}.${t.field}`;
    case "section":
      return t.section;
    case "indexed":
      return `${t.collection}[${t.index}]`;
    case "comment_anchor":
      return t.anchor;
    case "file":
      return t.file;
  }
}
