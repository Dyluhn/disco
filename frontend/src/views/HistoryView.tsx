import { AlertTriangle, MessageSquareText, Search, Trash2 } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { formatDate } from "@/lib/date";
import { useConversations, useDeleteConversation } from "@/hooks/useConversations";

/**
 * History (Prompt 5): the owner-scoped conversation library. Data flows through
 * hooks → the data layer (never a direct call); the list is filtered by owner by
 * the backend. Open routes to the main surface; delete is destructive and gated
 * by a confirm. Loading / error / empty are first-class, not afterthoughts.
 */
function SkeletonRows() {
  return (
    <ul aria-hidden className="flex flex-col">
      {[0, 1, 2, 3].map((i) => (
        <li key={i} className="flex flex-col gap-hair border-b border-hairline py-body last:border-b-0">
          <span className="h-3 w-2/3 animate-pulse rounded-full bg-surface-2" />
          <span className="h-2 w-24 animate-pulse rounded-full bg-surface-2" />
        </li>
      ))}
    </ul>
  );
}

function EmptyHistory() {
  const navigate = useNavigate();
  return (
    <div className="flex flex-col items-center gap-inline rounded-card border border-hairline bg-surface-1 p-major text-center">
      <MessageSquareText className="size-6 text-text-faint" aria-hidden />
      <div className="font-ui text-[0.95rem] font-medium text-text">No conversations yet</div>
      <p className="max-w-measure font-ui text-[0.85rem] text-text-muted">
        Answers you research will be kept here, tethered to their sources. Ask something to begin.
      </p>
      <button
        type="button"
        onClick={() => navigate("/")}
        className="mt-inline rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:text-text"
      >
        Start a query
      </button>
    </div>
  );
}

export function HistoryView() {
  const { data, isLoading, isError, refetch } = useConversations();
  const del = useDeleteConversation();
  const navigate = useNavigate();
  const [q, setQ] = useState("");

  const all = data ?? [];
  const filtered = all.filter((c) => c.title.toLowerCase().includes(q.trim().toLowerCase()));

  return (
    <div className="mx-auto w-full max-w-doc px-body py-section">
      <div className="mx-auto flex w-full max-w-[46rem] flex-col gap-section">
        <header>
          <h1 className="font-display text-[2rem] tracking-tight text-text">History</h1>
          <p className="font-ui text-[0.88rem] text-text-muted">
            Your past conversations — each answer kept with the sources it was grounded in.
          </p>
        </header>

        {/* search is shown whenever there's a populated list to filter */}
        {!isLoading && !isError && all.length > 0 && (
          <div className="flex items-center gap-inline rounded-control border border-hairline bg-surface-1 px-inline py-hair focus-within:border-hairline-strong">
            <Search className="size-4 shrink-0 text-text-faint" aria-hidden />
            <input
              type="search"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search conversations…"
              aria-label="Search conversations"
              className="w-full bg-transparent font-ui text-[0.88rem] text-text outline-none placeholder:text-text-faint"
            />
          </div>
        )}

        {isLoading && <SkeletonRows />}

        {isError && (
          <div
            role="alert"
            className="flex flex-col items-center gap-inline rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body text-center"
          >
            <AlertTriangle className="size-5 text-warn" aria-hidden />
            <div className="font-ui text-[0.88rem] text-text">Couldn't load your conversations.</div>
            <button
              type="button"
              onClick={() => refetch()}
              className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
            >
              Try again
            </button>
          </div>
        )}

        {!isLoading && !isError && all.length === 0 && <EmptyHistory />}

        {!isLoading && !isError && all.length > 0 && (
          <>
            {filtered.length === 0 ? (
              <p className="py-body font-ui text-[0.85rem] text-text-muted">
                No conversations match “{q}”.
              </p>
            ) : (
              <ul className="flex flex-col">
                {filtered.map((c) => (
                  <li
                    key={c.id}
                    className="flex items-center justify-between gap-section border-b border-hairline py-inline last:border-b-0"
                  >
                    {/* open → the main surface (loading the specific conversation
                        is a live-wire detail; the affordance navigates today) */}
                    <button
                      type="button"
                      onClick={() => navigate("/")}
                      className="group min-w-0 flex-1 text-left"
                    >
                      <div className="truncate font-ui text-[0.9rem] text-text transition-colors group-hover:text-accent">
                        {c.title}
                      </div>
                      <div className="font-ui text-[0.76rem] text-text-faint">
                        {formatDate(c.created_at)}
                      </div>
                    </button>
                    <ConfirmDialog
                      title="Delete this conversation?"
                      description="This permanently removes the conversation and its answer. This can't be undone."
                      confirmLabel="Delete"
                      onConfirm={() => del.mutate(c.id)}
                      trigger={
                        <button
                          type="button"
                          aria-label={`Delete conversation: ${c.title}`}
                          className="grid size-8 shrink-0 place-items-center rounded-control border border-hairline text-text-faint transition-colors hover:border-unsupported/50 hover:text-unsupported"
                        >
                          <Trash2 className="size-4" aria-hidden />
                        </button>
                      }
                    />
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </div>
  );
}
