import { AlertTriangle, CalendarClock, Loader2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { formatDate } from "@/lib/date";
import { useActivity } from "@/hooks/useActivity";
import type { RunningTask, ScheduleRunRecord } from "@/types/activity";

/** Where a running task opens — the surface that produced it. */
function surfacePath(t: RunningTask): string {
  if (t.surface === "deep_research") return `/deep/${t.id}`;
  if (t.surface === "agent") return `/agent/${t.id}`;
  if (t.surface === "build") return `/build/${t.id}`;
  return "/";
}

function StatusChip({ status }: { status?: string }) {
  const s = (status ?? "").toUpperCase();
  const tone =
    s === "RUNNING"
      ? "border-accent/40 text-accent"
      : s === "FINISHED"
        ? "border-supported/40 text-supported"
        : s === "STUCK" || s === "ERROR"
          ? "border-unsupported/40 text-unsupported"
          : "border-hairline text-text-faint";
  return (
    <span
      className={`shrink-0 rounded-full border px-hair font-ui text-[0.62rem] uppercase tracking-wide ${tone}`}
    >
      {status || "—"}
    </span>
  );
}

/**
 * Activity — the background-task dashboard. Two honest, live sections: what's
 * executing right now (the runtime's real in-flight tasks, polled) and the recent
 * scheduled-run history. Read-only; clicking a running task opens its surface
 * (view ≠ start). The "N running" count also drives the NavRail indicator.
 */
export function ActivityView() {
  const navigate = useNavigate();
  const { data, isLoading, isError, refetch } = useActivity();
  const running = data?.running ?? [];
  const recent = data?.recent_runs ?? [];

  return (
    <div className="mx-auto w-full max-w-doc px-body py-section">
      <div className="mx-auto flex w-full max-w-[46rem] flex-col gap-section">
        <header>
          <h1 className="font-display text-[2rem] tracking-tight text-text">Activity</h1>
          <p className="font-ui text-[0.88rem] text-text-muted">
            What's running right now, and the recent scheduled runs.
          </p>
        </header>

        {isLoading && (
          <div className="flex items-center gap-inline font-ui text-[0.86rem] text-text-faint">
            <Loader2 className="size-4 animate-spin" aria-hidden /> Loading activity…
          </div>
        )}

        {isError && (
          <div
            role="alert"
            className="flex flex-col items-center gap-inline rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body text-center"
          >
            <AlertTriangle className="size-5 text-warn" aria-hidden />
            <div className="font-ui text-[0.88rem] text-text">Couldn't load activity.</div>
            <button
              type="button"
              onClick={() => refetch()}
              className="min-h-11 rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text lg:min-h-0"
            >
              Try again
            </button>
          </div>
        )}

        {!isLoading && !isError && (
          <>
            {/* ---- Running now ---- */}
            <section className="flex flex-col gap-inline" data-running-count={running.length}>
              <h2 className="flex items-center gap-hair font-ui text-[0.95rem] font-medium text-text">
                Running now
                {running.length > 0 && (
                  <span className="rounded-full bg-accent/15 px-hair font-ui text-[0.7rem] text-accent">
                    {running.length}
                  </span>
                )}
              </h2>
              {running.length === 0 ? (
                <p className="font-ui text-[0.85rem] text-text-faint">
                  Nothing is running right now.
                </p>
              ) : (
                <ul className="flex flex-col">
                  {running.map((t) => (
                    <li
                      key={t.id}
                      className="border-b border-hairline py-inline last:border-b-0"
                    >
                      <button
                        type="button"
                        data-disco-control="activity.open-task"
                        data-task-id={t.id}
                        onClick={() => navigate(surfacePath(t))}
                        className="group flex min-h-11 w-full items-center gap-inline text-left lg:min-h-0"
                      >
                        <Loader2
                          className="size-4 shrink-0 animate-spin text-accent"
                          aria-hidden
                        />
                        <span className="min-w-0 flex-1">
                          <span className="flex items-center gap-hair">
                            <span className="truncate font-ui text-[0.9rem] text-text transition-colors group-hover:text-accent">
                              {t.title}
                            </span>
                            <StatusChip status={t.status} />
                            {t.surface && (
                              <span className="shrink-0 rounded-full border border-hairline px-hair font-ui text-[0.62rem] uppercase tracking-wide text-text-faint">
                                {t.surface === "deep_research" ? "deep" : t.surface}
                              </span>
                            )}
                          </span>
                          {t.created_at && (
                            <span className="font-ui text-[0.76rem] text-text-faint">
                              Started {formatDate(t.created_at)}
                            </span>
                          )}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {/* ---- Recent scheduled runs ---- */}
            <section className="flex flex-col gap-inline">
              <h2 className="font-ui text-[0.95rem] font-medium text-text">
                Recent scheduled runs
              </h2>
              {recent.length === 0 ? (
                <p className="font-ui text-[0.85rem] text-text-faint">
                  No scheduled runs have fired yet. Schedule a conversation to re-run on a
                  cadence and they'll show up here.
                </p>
              ) : (
                <ul className="flex flex-col" data-scheduled-runs>
                  {recent.map((r: ScheduleRunRecord) => (
                    <li
                      key={r.run_id}
                      data-run-id={r.run_id}
                      className="flex items-center gap-inline border-b border-hairline py-inline last:border-b-0"
                    >
                      <CalendarClock className="size-4 shrink-0 text-text-faint" aria-hidden />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-hair">
                          <span className="truncate font-ui text-[0.88rem] text-text">
                            {r.description || r.title || "(scheduled run)"}
                          </span>
                          {r.coalesced === 1 && (
                            <span
                              title="Missed fires were coalesced into one catch-up run"
                              className="shrink-0 rounded-full border border-hairline px-hair font-ui text-[0.62rem] uppercase tracking-wide text-text-faint"
                            >
                              coalesced
                            </span>
                          )}
                        </span>
                        <span className="font-ui text-[0.76rem] text-text-faint">
                          {formatDate(r.fired_at)}
                          {r.title && r.description ? ` · ${r.title}` : ""}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
}
