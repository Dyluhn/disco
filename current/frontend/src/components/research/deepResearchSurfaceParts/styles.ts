/**
 * Button token constants for the Deep Research top bar. Relocated verbatim
 * from DeepResearchSurface.tsx (was module-private) — see that file's header
 * for the surface map.
 */

// Color-free base so state variants can SWAP the border/text colors instead of
// appending a second, competing set (Tailwind picks the winner by stylesheet
// order, not source order — appending `text-accent` onto `text-text-muted` is a
// coin-flip). Every variant below starts from this base and adds exactly one
// border-/text- set.
// min-h-11 (44px) is a mobile touch-target floor; lg:min-h-0 restores the
// original auto height so desktop stays pixel-identical.
export const CTRL_BTN_BASE =
  "flex min-h-11 items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.78rem] transition-colors lg:min-h-0";
export const CTRL_BTN = `${CTRL_BTN_BASE} border-hairline text-text-muted hover:text-text`;
// Notify armed: a filled accent chip that reads as "on" at a glance (mirrors the
// SourcePicker active chip). Full swap — NOT appended onto CTRL_BTN.
export const NOTIFY_ARMED_BTN = `${CTRL_BTN_BASE} border-accent/50 bg-accent/10 text-accent`;
// Kill is destructive (ends the run for good) → warn-tinted, distinct from Stop.
export const KILL_BTN =
  "flex min-h-11 items-center gap-hair rounded-control border border-warn/40 px-inline py-hair font-ui text-[0.78rem] text-warn transition-colors hover:bg-warn/10 lg:min-h-0";
// A control that exists but is not yet wired — visibly inert (no hover, dimmed,
// not-allowed cursor), never a click that silently errors. NO FALSE AFFORDANCES.
export const PENDING_BTN =
  "flex min-h-11 items-center gap-hair rounded-control border border-hairline border-dashed px-inline py-hair font-ui text-[0.78rem] text-text-faint opacity-50 cursor-not-allowed lg:min-h-0";
