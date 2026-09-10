/**
 * One name per depth tier, everywhere a person sees one.
 *
 * The tier IDS are wire-level (the create frame, telemetry, the durable run
 * record) and never change. The NAMES had drifted into three vocabularies for
 * the same setting: the depth menu said "Thorough", the report banner offered
 * "Run on Exhaustive tier", and the report badge printed the raw id as
 * "TIER: QUICK". A reader has no way to know those are one control (UI-21), so
 * every user-facing tier name now comes from this map — the menu's names win,
 * because that is where the choice is made.
 */
export type DepthTier = "quick" | "standard_deep" | "exhaustive";

export const DEPTH_TIER_LABEL: Record<DepthTier, string> = {
  quick: "Quick",
  standard_deep: "Standard",
  exhaustive: "Thorough",
};

/**
 * The name for a tier string that arrived off the wire.
 *
 * A report replayed from an older run can carry a tier this build does not
 * know; showing its raw id is better than showing nothing, and it is the only
 * case that can reach the fallback.
 */
export function depthTierLabel(tier: string): string {
  return DEPTH_TIER_LABEL[tier as DepthTier] ?? tier;
}
