import { Bot, Code, FileSearch, Search } from "lucide-react";
import { useMemo, type ComponentType } from "react";
import { getSuggestions, type SuggestionSurface } from "@/data/suggestions";

type IconComponent = ComponentType<{ className?: string; "aria-hidden"?: boolean }>;

const ICON_BY_SURFACE: Record<SuggestionSurface, IconComponent> = {
  search: Search,
  deep_research: FileSearch,
  build: Code,
  agent: Bot,
};

const CONTROL_BY_SURFACE: Record<SuggestionSurface, string> = {
  search: "search.suggestion",
  deep_research: "deep.suggestion",
  build: "build.suggestion",
  agent: "agent.suggestion",
};

export function SuggestionChips({
  surface,
  onPick,
}: {
  surface: SuggestionSurface;
  onPick: (text: string) => void;
}) {
  const suggestions = useMemo(() => getSuggestions(surface), [surface]);
  const Icon = ICON_BY_SURFACE[surface];

  if (suggestions.length === 0) return null;

  return (
    <div
      className="flex w-full max-w-measure flex-wrap justify-center gap-inline"
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
          className="flex max-w-full items-start gap-hair rounded-control border border-hairline px-body py-inline text-left font-ui text-[0.82rem] leading-snug text-text-muted transition-colors hover:border-hairline-strong hover:text-text sm:max-w-[34rem]"
        >
          <Icon className="mt-px size-3.5 shrink-0 text-text-faint" aria-hidden />
          <span className="min-w-0 whitespace-normal">{suggestion.text}</span>
        </button>
      ))}
    </div>
  );
}
