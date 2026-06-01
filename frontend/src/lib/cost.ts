import { isFree, type ModelInfo } from "@/types/models";

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
