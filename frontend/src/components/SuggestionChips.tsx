import { useMemo } from "react";
import { getSuggestions, type SuggestionSurface } from "@/data/suggestions";

const CONTROL_BY_SURFACE: Record<SuggestionSurface, string> = {
  search: "search.suggestion",
  deep_research: "deep.suggestion",
  build: "build.suggestion",
  agent: "agent.suggestion",
};

/** Quiet, uniform prompt pills under the composer. No icons, no heavy borders —
 * a single centered row (wrapping to a second at narrow widths) of ghost pills
 * that read as whispers, not buttons competing with the composer. Capped at 4. */
export function SuggestionChips({
  surface,
  onPick,
}: {
  surface: SuggestionSurface;
  onPick: (text: string) => void;
}) {
  const suggestions = useMemo(() => getSuggestions(surface).slice(0, 4), [surface]);

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
