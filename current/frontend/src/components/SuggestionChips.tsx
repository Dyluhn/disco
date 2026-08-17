import { useEffect, useMemo, useState } from "react";
import { fetchSuggestionPrompts } from "@/api/suggestions";
import {
  getSuggestions,
  sampleSuggestions,
  type Suggestion,
  type SuggestionSurface,
} from "@/data/suggestions";

const CONTROL_BY_SURFACE: Record<SuggestionSurface, string> = {
  search: "search.suggestion",
  deep_research: "deep.suggestion",
  build: "build.suggestion",
  agent: "agent.suggestion",
};

/** Quiet prompt pills under a composer. Deep Research uses three because its
 * splash carries an extra depth selector; the other surfaces keep four. */
export function SuggestionChips({
  surface,
  onPick,
}: {
  surface: SuggestionSurface;
  onPick: (text: string) => void;
}) {
  const limit = surface === "deep_research" ? 3 : 4;
  const fallbackSuggestions = useMemo(() => getSuggestions(surface), [surface]);
  const [generated, setGenerated] = useState<{
    surface: SuggestionSurface;
    suggestions: Suggestion[];
  } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void fetchSuggestionPrompts(surface, controller.signal)
      .then((payload) => {
        const suggestions = payload.suggestions.map((text, index) => ({
          id: `${surface}-generated-${index + 1}`,
          text,
        }));
        if (payload.source === "generated" && suggestions.length >= limit) {
          setGenerated({
            surface,
            suggestions: sampleSuggestions(suggestions, suggestions.length),
          });
        }
      })
      .catch(() => {
        // The curated pool is already rendered; generation is opportunistic.
      });
    return () => controller.abort();
  }, [surface, limit]);

  const suggestions = (
    generated?.surface === surface ? generated.suggestions : fallbackSuggestions
  ).slice(0, limit);

  if (suggestions.length === 0) return null;

  return (
    <div
      className="mx-auto flex w-full max-w-[46rem] flex-wrap items-center justify-center gap-x-2 gap-y-2"
      data-suggestion-surface={surface}
    >
      {suggestions.map((suggestion, index) => (
        <button
          key={suggestion.id}
          type="button"
          onClick={() => onPick(suggestion.text)}
          data-disco-control={CONTROL_BY_SURFACE[surface]}
          data-suggestion-id={suggestion.id}
          data-suggestion-index={index}
          title={suggestion.text}
          className="max-w-[21rem] truncate rounded-full border border-hairline bg-transparent px-4 py-1.5 font-ui text-[0.8rem] text-text-faint transition-colors hover:bg-surface-1 hover:text-text-muted"
        >
          {suggestion.text}
        </button>
      ))}
    </div>
  );
}
