/**
 * NeedMoreCard — shared button style tokens (extracted verbatim from
 * NeedMoreCard.tsx). Match CTRL_BTN in DeepResearchSurface.tsx.
 */

// min-h-11 (44px) is a mobile touch-target floor; lg:min-h-0 restores the
// original auto height so desktop stays pixel-identical.
export const CTRL_BTN =
  "flex min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text lg:min-h-0";

export const ACTIVE_BTN =
  "flex min-h-11 items-center gap-hair rounded-control border border-accent/60 bg-accent/5 px-inline py-hair font-ui text-[0.78rem] text-accent transition-colors hover:border-accent lg:min-h-0";
