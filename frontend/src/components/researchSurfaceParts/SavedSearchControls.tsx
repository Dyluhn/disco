import type { ResearchController } from "@/hooks/useResearch";

export function SavedSearchControls({ controller: r }: { controller: ResearchController }) {
  return <>
    {r.turns?.length > 1 && (
      <nav aria-label="Saved answers" className="mx-auto flex w-full max-w-doc flex-wrap gap-inline px-body">
        {r.turns.map((turn, index) => (
          <button key={turn.scope.request_id} type="button"
            data-disco-control="search.saved-answer"
            aria-pressed={r.scope?.request_id === turn.scope.request_id}
            disabled={r.phase === "running"}
            onClick={() => r.selectTurn(turn.scope.request_id!)}
            className="rounded-control border border-hairline px-inline py-hair text-text-muted">
            {index + 1}. {turn.scope.query}
          </button>
        ))}
      </nav>
    )}
    {r.submitError && <p role="alert" className="px-body text-unsupported">{String(r.submitError)}</p>}
  </>;
}
