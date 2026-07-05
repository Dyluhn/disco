import { Database } from "lucide-react";
import { useEffect, useMemo } from "react";
import { useSpaces } from "@/hooks/useSpaces";
import { cn } from "@/lib/cn";

export function SpaceGroundingControl({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const { data } = useSpaces();
  const spaces = useMemo(() => data?.spaces ?? [], [data?.spaces]);

  useEffect(() => {
    if (spaces.length === 0 || selected.length === 0) return;
    const valid = new Set(spaces.map((space) => space.space_id));
    const kept = selected.filter((id) => valid.has(id));
    if (kept.length !== selected.length) onChange(kept);
  }, [spaces, selected, onChange]);

  if (spaces.length === 0) return null;

  const toggle = (spaceId: string) => {
    onChange(
      selected.includes(spaceId)
        ? selected.filter((id) => id !== spaceId)
        : [...selected, spaceId],
    );
  };

  return (
    <div className="flex min-w-0 items-center gap-hair" aria-label="Ground in spaces">
      <span className="flex shrink-0 items-center gap-hair font-ui text-[0.74rem] text-text-faint">
        <Database className="size-3.5" aria-hidden />
        Ground in:
      </span>
      <div className="flex min-w-0 flex-wrap items-center gap-hair">
        {spaces.map((space) => {
          const active = selected.includes(space.space_id);
          return (
            <button
              key={space.space_id}
              type="button"
              aria-pressed={active}
              data-disco-control="spaces.grounding-chip"
              onClick={() => toggle(space.space_id)}
              className={cn(
                "max-w-36 truncate rounded-control border px-inline py-hair font-ui text-[0.76rem] transition-colors",
                active
                  ? "border-accent/50 bg-accent/10 text-accent"
                  : "border-hairline text-text-muted hover:text-text",
              )}
              title={space.name}
            >
              {space.name}
            </button>
          );
        })}
      </div>
    </div>
  );
}
