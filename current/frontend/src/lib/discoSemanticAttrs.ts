/**
 * discoSemanticAttrs — the data-disco-* attribute NAME constants (P8C).
 *
 * The Python core `DataDiscoAttr` (current/packages/core/.../appkit/semantic_metadata.py, P8B) is
 * the CANONICAL vocabulary; this file is a checked-in MIRROR for the TS frontend (which
 * cannot import Python). A Python drift-guard test
 * (current/packages/core/tests/test_semantic_attr_parity.py) asserts these values stay EXACTLY in
 * sync with `DataDiscoAttr` — so they can never silently diverge.
 *
 * @module discoSemanticAttrs
 */

export const DISCO_ATTR = {
  FIELD: "data-disco-field",
  SECTION: "data-disco-section",
  FILE: "data-disco-file",
  FLOW: "data-disco-flow",
  SCREEN_LABEL: "data-disco-screen-label",
  COMMENT_ANCHOR: "data-disco-comment-anchor",
  METRIC_ID: "data-disco-metric-id",
  VERSION: "data-disco-version",
  COLLECTION: "data-disco-collection",
  INDEX: "data-disco-index",
  ITEM_KIND: "data-disco-item-kind",
} as const;

export type DiscoAttrName = (typeof DISCO_ATTR)[keyof typeof DISCO_ATTR];
