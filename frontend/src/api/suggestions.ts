import { agentFetch } from "@/api/client";
import type { SuggestionSurface } from "@/data/suggestions";

type ApiSuggestionSurface = "research" | "build" | "agent";

export interface SuggestionResponse {
  surface: ApiSuggestionSurface;
  suggestions: string[];
  source: "generated" | "curated";
}

function apiSurface(surface: SuggestionSurface): ApiSuggestionSurface {
  if (surface === "build" || surface === "agent") return surface;
  return "research";
}

export async function fetchSuggestionPrompts(
  surface: SuggestionSurface,
  signal?: AbortSignal,
): Promise<SuggestionResponse> {
  const params = new URLSearchParams({ surface: apiSurface(surface) });
  const res = await agentFetch(`/api/suggestions?${params.toString()}`, {
    headers: { accept: "application/json" },
    signal,
  });
  if (!res.ok) throw new Error(`suggestions request failed: ${res.status}`);
  const payload = (await res.json()) as Partial<SuggestionResponse>;
  if (!Array.isArray(payload.suggestions)) {
    throw new Error("suggestions response missing suggestions");
  }
  return {
    surface: apiSurface(surface),
    suggestions: payload.suggestions.filter(
      (item): item is string => typeof item === "string",
    ),
    source: payload.source === "generated" ? "generated" : "curated",
  };
}
