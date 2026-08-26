import {
  BookOpen,
  GraduationCap,
  Newspaper,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/cn";

// The configured web provider is owned by Settings. These are per-run
// additions only; an empty selection keeps the configured provider unchanged.
type SourceId = "arxiv" | "news" | "semantic_scholar";

const SOURCES: Array<{ id: SourceId; label: string; Icon: LucideIcon }> = [
  { id: "news", label: "News", Icon: Newspaper },
  { id: "arxiv", label: "arXiv", Icon: BookOpen },
  { id: "semantic_scholar", label: "Semantic Scholar", Icon: GraduationCap },
];

export function SourcePicker({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const toggle = (sourceId: SourceId) => {
    onChange(
      selected.includes(sourceId)
        ? selected.filter((id) => id !== sourceId)
        : [...selected, sourceId],
    );
  };

  return (
    <div className="flex min-w-0 items-center gap-hair" aria-label="Research sources">
      <span className="shrink-0 font-ui text-[0.76rem] text-text-faint">
        Additional sources
      </span>
      <div className="flex min-w-0 flex-wrap items-center gap-hair">
        {SOURCES.map(({ id, label, Icon }) => {
          const active = selected.includes(id);
          return (
            <button
              key={id}
              type="button"
              aria-pressed={active}
              data-disco-control="research.source-chip"
              data-source-id={id}
              onClick={() => toggle(id)}
              className={cn(
                "inline-flex min-h-11 items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.76rem] transition-colors lg:min-h-0",
                active
                  ? "border-accent/50 bg-accent/10 text-accent"
                  : "border-hairline text-text-muted hover:text-text",
              )}
              title={label}
            >
              <Icon className="size-3.5" aria-hidden />
              {label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
