import { BookOpen, Globe2, GraduationCap, Search, type LucideIcon } from "lucide-react";
import { useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import { useDataSourcesConfig } from "@/hooks/useModels";
import { cn } from "@/lib/cn";

type SourceId =
  | "ddgs"
  | "arxiv"
  | "semantic_scholar"
  | "tavily"
  | "brave"
  | "searxng";

const KEYLESS = new Set<SourceId>(["ddgs", "arxiv", "semantic_scholar"]);

const SOURCES: Array<{
  id: SourceId;
  label: string;
  Icon: LucideIcon;
  needsSetup?: boolean;
}> = [
  { id: "ddgs", label: "Web", Icon: Globe2 },
  { id: "arxiv", label: "arXiv", Icon: BookOpen },
  { id: "semantic_scholar", label: "Semantic Scholar", Icon: GraduationCap },
  { id: "tavily", label: "Tavily", Icon: Search, needsSetup: true },
  { id: "brave", label: "Brave", Icon: Search, needsSetup: true },
  { id: "searxng", label: "SearXNG", Icon: Search, needsSetup: true },
];

export function SourcePicker({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const { data } = useDataSourcesConfig();
  const configured = useMemo(
    () => new Set(data?.configured_sources ?? []),
    [data?.configured_sources],
  );
  const enabledIds = useMemo(
    () =>
      new Set(
        SOURCES.filter((source) => KEYLESS.has(source.id) || configured.has(source.id)).map(
          (source) => source.id,
        ),
      ),
    [configured],
  );

  useEffect(() => {
    const kept = selected.filter((id) => enabledIds.has(id as SourceId));
    if (kept.length !== selected.length) onChange(kept);
  }, [enabledIds, selected, onChange]);

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
        {SOURCES.map(({ id, label, Icon, needsSetup }) => {
          const enabled = enabledIds.has(id);
          const active = selected.includes(id);
          return (
            <span key={id} className="inline-flex items-center gap-hair">
              <button
                type="button"
                aria-pressed={active}
                disabled={!enabled}
                data-disco-control="research.source-chip"
                data-source-id={id}
                onClick={() => toggle(id)}
                className={cn(
                  "inline-flex items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.76rem] transition-colors",
                  enabled && active
                    ? "border-accent/50 bg-accent/10 text-accent"
                    : enabled
                      ? "border-hairline text-text-muted hover:text-text"
                      : "cursor-not-allowed border-hairline border-dashed text-text-faint opacity-60",
                )}
                title={!enabled && needsSetup ? `${label} needs setup in Settings` : label}
              >
                <Icon className="size-3.5" aria-hidden />
                {label}
              </button>
              {!enabled && needsSetup && (
                <Link
                  to="/settings#data-sources"
                  className="font-ui text-[0.72rem] text-accent hover:underline"
                >
                  Add in Settings -&gt;
                </Link>
              )}
            </span>
          );
        })}
      </div>
    </div>
  );
}
