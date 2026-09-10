import type { DriverModel } from "@/types/agent";
import { isFree, isPricingUnknown, isSubscription, type ModelInfo, type TokenUsage } from "@/types/models";

/**
 * Cost legibility: every place a model is chosen shows its spend. Free → "Free";
 * subscription → "Subscription" (flat plan, no per-token rate — W-05); metered →
 * "$in / $out / Mtok". Kept terse for the pill/matrix where space is tight.
 */
export function costLabel(m: ModelInfo): string {
  if (isSubscription(m)) return "Subscription";
  if (isPricingUnknown(m)) return "Pricing unknown";
  if (isFree(m)) return "Free";
  return `$${m.price_in_per_m} / $${m.price_out_per_m} / Mtok`;
}

/** A one-word cost tag for very tight spots (the pill face). */
export function costTag(m: ModelInfo): string {
  if (isSubscription(m)) return "Subscription";
  // UI-38: pricing_mode "unknown" = the provider catalogue reported no rate. It is
  // the SAME stored state the driver payload carries as `free: false`, which the
  // Build picker already labels "Paid" (see driverCostTag) — so say "Paid" here too
  // and never print the bare word "Unknown" beside a model name. The roomier
  // `costLabel` still spells out "Pricing unknown" where there is space for it.
  if (isPricingUnknown(m)) return "Paid";
  return isFree(m) ? "Free" : `$${m.price_in_per_m}/Mtok`;
}

/**
 * One-word cost tag for the Build driver picker. A DriverModel carries no per-token
 * price (just `free` + `pricing_mode`), so this is the picker's counterpart to
 * `costTag` — a single DISPLAY label, never the raw lowercase enum value (W-05):
 * subscription → "Subscription", free → "Free", else "Paid". Capitalized everywhere.
 */
export function driverCostTag(m: DriverModel): string {
  if (m.pricing_mode === "subscription") return "Subscription";
  return m.free ? "Free" : "Paid";
}

/**
 * Calculate USD cost for a given usage and model.
 * Note: prices are per-million tokens.
 */
export function calculateUsageCost(m: ModelInfo, usage: TokenUsage): number {
  // Free AND subscription models have no per-token bill (W-05): a subscription is a
  // flat plan fee, so there is no usage-derived USD cost to meter.
  // Unknown pricing: no rates exist to meter against — 0, and the cost surfaces
  // label the model "pricing unknown" rather than implying a measured $0.
  if (isFree(m) || isSubscription(m) || isPricingUnknown(m)) return 0;
  // If the backend already calculated cost_usd (v1.2 router does this for OpenRouter),
  // use it as the source of truth; otherwise calculate from tokens.
  if (usage.cost_usd && usage.cost_usd > 0) return usage.cost_usd;

  const inputCost = (usage.input_tokens * m.price_in_per_m) / 1_000_000;
  const outputCost = (usage.output_tokens * m.price_out_per_m) / 1_000_000;
  return inputCost + outputCost;
}

/** Format USD for the meter. */
export function formatCost(usd: number): string {
  if (usd === 0) return "$0.00";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}
