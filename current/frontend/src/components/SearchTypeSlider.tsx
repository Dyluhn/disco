import { BookOpen, Search } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ScopeId } from "@/shell/mode";

/**
 * The in-composer search-type slider — a two-segment pill (Search · Deep
 * Research) that lives INSIDE the query card's control row, replacing the
 * standalone "Search type" row that used to sit above the box. Same segmented
 * grammar as the shell's ModeSlider, scoped to the Search surface's two
 * scopes. The wire-level scope ids ("standard" / "deep_research") are
 * unchanged — this is a control relocation, not a behavior change.
 */

type SearchType = Extract<ScopeId, "standard" | "deep_research">;

const SEGMENTS: Array<{ id: SearchType; label: string; icon: LucideIcon; controlId: string }> = [
  { id: "standard", label: "Search", icon: Search, controlId: "search.type-standard" },
  { id: "deep_research", label: "Deep Research", icon: BookOpen, controlId: "search.type-deep-research" },
];

interface Props {
  value: SearchType;
  onChange: (next: ScopeId) => void;
}

export function SearchTypeSlider({ value, onChange }: Props) {
  return (
    <div
      role="radiogroup"
      aria-label="Search type"
      className="inline-flex items-center rounded-full border border-hairline bg-surface-1 p-px"
    >
      {SEGMENTS.map((s) => {
        const active = s.id === value;
        const Icon = s.icon;
        return (
          <button
            key={s.id}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={s.label}
            data-disco-control={s.controlId}
            data-active={active}
            onClick={() => {
              if (s.id !== value) onChange(s.id);
            }}
            className={cn(
              "flex min-h-11 items-center gap-hair rounded-full px-inline py-hair font-ui text-[0.76rem] transition-colors lg:min-h-0",
              active ? "bg-surface-2 text-text" : "text-text-muted hover:text-text",
            )}
          >
            <Icon className="size-3.5 shrink-0 text-text-faint" aria-hidden />
            {s.label}
          </button>
        );
      })}
    </div>
  );
}

export type { SearchType };
