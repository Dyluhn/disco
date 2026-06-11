import { isFree, type ModelInfo, type TokenUsage } from "@/types/models";

/**
 * Cost legibility: every place a model is chosen shows its spend. Local → "Free";
 * paid → "$in / $out / Mtok". Kept terse for the pill/matrix where space is tight.
 */
export function costLabel(m: ModelInfo): string {
  if (isFree(m)) return "Free";
  return `$${m.price_in_per_m} / $${m.price_out_per_m} / Mtok`;
}

/** A one-word cost tag for very tight spots (the pill face). */
export function costTag(m: ModelInfo): string {
  return isFree(m) ? "Free" : `$${m.price_in_per_m}/Mtok`;
}

/**
 * Calculate USD cost for a given usage and model.
 * Note: prices are per-million tokens.
 */
export function calculateUsageCost(m: ModelInfo, usage: TokenUsage): number {
  if (isFree(m)) return 0;
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
