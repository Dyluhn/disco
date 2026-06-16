/**
 * Barrel for the per-block renderers. The individual block components now live
 * under `./blocks/` (one cohesive file each); this module re-exports the same
 * public surface so existing importers (Markdown.tsx, AnswerDocument.tsx, the
 * block tests) are unaffected. Pure structural split — no behavior change.
 */
export { CitationMarker, CitedText } from "./blocks/CitedText";
export { ChartBlockComponent } from "./blocks/ChartBlock";
export { BlockView } from "./blocks/BlockView";
