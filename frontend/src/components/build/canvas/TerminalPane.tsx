import { useEffect, useMemo, useRef } from "react";
import { cn } from "@/lib/cn";
import { deriveTerminal } from "@/lib/buildTrace";
import type { SessionInfo, SessionView } from "@/hooks/useSessions";
import type { AgentEvent } from "@/types/agent";
import { Empty } from "./Empty";

export function TerminalHistoryView({ events }: { events: AgentEvent[] }) {
  const entries = useMemo(() => deriveTerminal(events), [events]);
  if (entries.length === 0)
    return <Empty>No commands run yet. Shell + code output streams here.</Empty>;
  return (
    <div className="h-full overflow-auto bg-[oklch(0.15_0.005_260)] px-body py-inline font-mono text-[0.78rem] leading-relaxed">
      {entries.map((e) => (
        <div key={e.id} className="mb-inline">
          <div className="flex items-center gap-hair text-supported">
            <span className="text-text-faint">$</span>
            <span className="text-[oklch(0.85_0.02_150)]">{e.command}</span>
            {e.running && <span className="animate-pulse text-text-faint">▋</span>}
          </div>
          {e.output && (
            <pre
              className={cn(
                "mt-px whitespace-pre-wrap",
                e.success ? "text-[oklch(0.78_0.005_260)]" : "text-unsupported",
              )}
            >
              {e.output.length > 4000 ? e.output.slice(0, 4000) + "\n…" : e.output}
            </pre>
          )}
        </div>
      ))}
    </div>
  );
}

export function TerminalPane({
  events,
  sessions,
  selectedName,
  setSelectedName,
  view,
}: {
  events: AgentEvent[];
  sessions: SessionInfo[];
  selectedName: string | null;
  setSelectedName: (name: string) => void;
  view: SessionView | null;
}) {
  // Purely presentational: the useSessions hook lives in ExecutionCanvas (NOT
  // here) because Radix unmounts inactive tab content — if polling lived in
  // this pane, the tab-label busy dot could never light up while the user is
  // on another tab (its whole purpose), and would freeze stale on switch-away.

  // Near-bottom autoscroll: only follow the tail when the user hasn't scrolled away.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);
  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }
  useEffect(() => {
    if (atBottomRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [view?.content]);

  if (sessions.length === 0) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="min-h-0 flex-1">
          <TerminalHistoryView events={events} />
        </div>
        <div className="shrink-0 border-t border-hairline px-body py-hair font-mono text-[0.72rem] text-text-faint">
          history (no live sessions)
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-[oklch(0.15_0.005_260)]">
      {/* Session chips */}
      <div className="flex shrink-0 items-center gap-hair overflow-x-auto border-b border-white/10 px-body py-hair">
        {sessions.map((s) => (
          <button
            key={s.name}
            type="button"
            onClick={() => setSelectedName(s.name)}
            className={cn(
              "flex min-h-11 shrink-0 items-center gap-hair rounded px-inline py-px font-mono text-[0.72rem] transition-colors lg:min-h-0",
              s.name === selectedName
                ? "bg-white/10 text-text"
                : "text-text-faint hover:text-text",
            )}
          >
            <span
              className={cn(
                "size-1.5 rounded-full",
                s.busy
                  ? "animate-pulse bg-[oklch(0.75_0.18_150)]"
                  : "bg-[oklch(0.55_0.08_150)]",
              )}
              aria-label={s.busy ? "running" : "idle"}
            />
            {s.name}
          </button>
        ))}
      </div>
      {/* Live content pane */}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-auto px-body py-inline"
      >
        <pre className="whitespace-pre-wrap font-mono text-[0.78rem] leading-relaxed text-[oklch(0.78_0.005_260)]">
          {view?.content ?? ""}
        </pre>
      </div>
    </div>
  );
}
