import {
  BookOpen,
  Globe2,
  GraduationCap,
  Newspaper,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/cn";

// Only the KEYLESS, always-available federation sources live here. The general
// web-search PROVIDER (ddgs / tavily / brave / searxng) is a single choice owned
// entirely by Settings → Data sources — exposing it here as per-query chips
// duplicated that setting and let a chip silently override "the one they set".
// So: no provider chips, no "needs setup" / "Add in Settings" affordance. An
// EMPTY selection (the default) means "use my configured provider"; picking any
// of these federates that keyless source on top for this one query.
type SourceId = "ddgs" | "arxiv" | "news" | "semantic_scholar";

const SOURCES: Array<{ id: SourceId; label: string; Icon: LucideIcon }> = [
  { id: "ddgs", label: "Web", Icon: Globe2 },
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
      <span className="shrink-0 font-ui text-[0.74rem] text-text-faint">
        Sources
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
