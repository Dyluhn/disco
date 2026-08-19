import { ArrowUpRight } from "lucide-react";

/** Context-aware follow-up suggestions (from the backend) as quiet pills. */
export function FollowUps({ items, onPick }: { items: string[]; onPick: (q: string) => void }) {
  if (items.length === 0) return null;
  return (
    <div className="mx-auto flex w-full max-w-measure flex-col gap-inline px-body">
      <div className="font-ui text-[0.7rem] font-semibold uppercase tracking-wide text-text-faint">
        Follow up
      </div>
      <div className="flex flex-wrap gap-inline">
        {items.map((q, i) => (
          <button
            key={q}
            type="button"
            onClick={() => onPick(q)}
            data-disco-control="search.followup"
            data-followup-index={i}
            className="flex min-h-11 items-center gap-hair rounded-control border border-hairline bg-surface-1 px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text lg:min-h-0"
          >
            {q}
            <ArrowUpRight className="size-3 text-text-faint" aria-hidden />
          </button>
        ))}
      </div>
    </div>
  );
}
