import type { ProviderCatalogueModel } from "@/types/models";

/** Minimal shape `errorText` needs from a caught error — deliberately NOT the
 * `ApiError` class itself: parts must never import `@/api/client` (that seam
 * stays owned by the parent), so the parent hands down a type-guard closing
 * over the real `ApiError` check instead of the class. */
export type ErrorLike = { message: string };
export type ApiErrorPredicate = (error: unknown) => error is ErrorLike;

export function errorText(error: unknown, isApiError: ApiErrorPredicate): string {
  if (isApiError(error)) {
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
