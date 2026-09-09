import { isApiFailure } from "@/api/errors";
import type { ProviderCatalogueModel, ProviderInfo } from "@/types/models";

export function errorText(error: unknown): string {
  if (isApiFailure(error)) {
    try {
      const parsed = JSON.parse(error.message) as { detail?: string };
      return parsed.detail || error.message;
    } catch {
      return error.message;
    }
  }
  return "Request failed.";
}

export function hostLabel(baseUrl: string): string {
  try {
    return new URL(baseUrl).host;
  } catch {
    return baseUrl.replace(/^https?:\/\//, "").split("/")[0] || baseUrl;
  }
}

export function contextLabel(value: number | null | undefined): string {
  if (!value) return "context unknown";
  return value >= 1000 ? `${Math.floor(value / 1000)}K ctx` : `${value} ctx`;
}

export function priceLabel(m: ProviderCatalogueModel): string {
  if (m.price_in_per_m == null || m.price_out_per_m == null) {
    return "pricing unknown";
  }
  if (m.price_in_per_m === 0 && m.price_out_per_m === 0) return "Free";
  return `$${m.price_in_per_m.toFixed(2)} / $${m.price_out_per_m.toFixed(2)} / Mtok`;
}

export type KeyBadge = {
  text: string;
  tone: "ok" | "bad" | "neutral";
  /** the provider's own answer, shown under the row when it refused */
  detail?: string;
};

/** What the row may honestly claim about a provider's key.
 *
 * A stored key is not a working key: before this, any stored value showed a
 * green "Key ready" and a typo'd key looked configured (UI-34). "Key ready" now
 * requires a /models probe the provider actually answered. */
export function keyBadge(provider: ProviderInfo): KeyBadge {
  if (provider.requires_api_key === false) return { text: "No key needed", tone: "ok" };
  if (!provider.has_key) return { text: "No usable key", tone: "bad" };
  if (provider.key_verified === true) return { text: "Key ready", tone: "ok" };
  if (provider.key_verified === false) {
    return {
      text: "Key rejected",
      tone: "bad",
      detail: provider.key_error ?? undefined,
    };
  }
  return { text: "Key stored, not verified", tone: "neutral" };
}

/** What to offer when a provider's catalogue doesn't report a context window.
 * 128k is the current table stakes for served models, and the field stays
 * editable — asking with an empty box made the user guess a number they had no
 * way to know (UI-3). */
export const DEFAULT_CONTEXT_WINDOW = 131072;
