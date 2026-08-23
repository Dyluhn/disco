/**
 * DeepProgressStrip — the live-progress treatment during a Deep Research
 * run. Three components inside the strip:
 *
 *   ┌─────────────────────────────────────────────────────────────┐
 *   │ 12 searches  ·  30 sources  ·  round 2/4  ·  4m       [ ▾ ]  │  ← collapsible header
 *   ├─────────────────────────────────────────────────────────────┤
 *   │ Searching: "SK On pilot production timeline"                │  ← heartbeat
 *   ├─────────────────────────────────────────────────────────────┤
 *   │ The brief: how the model read the question and the angles   │  ← the brief
 *   │ it intends to chase.                                        │
 *   ├─────────────────────────────────────────────────────────────┤
 *   │ Searching: "SK On pilot production timeline"                │
 *   │   Round 2: +6 sources (12 total for this sub-question)      │  ← ActivityFeed
 *   │ Framed the question                                          │     (the live trace)
 *   └─────────────────────────────────────────────────────────────┘
 *
 * v2 (gateless deep research): there is no plan, so the strip no longer
 * renders a sub-question checklist — a checklist of steps the run never
 * promised was the dishonest part of the old treatment. What replaces it is
 * the model's own brief plus the real event counts.
 *
 * When the run finishes the strip collapses to a single line: "How it
 * researched · 12 searches · 30 sources · [ ▸ Expand ]". Click to re-open the
 * full trace (transparency preserved, not discarded).
 */

import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { useState } from "react";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { cn } from "@/lib/cn";
import type { ActivityItem } from "@/lib/buildTrace";
import type { DeepStats } from "@/lib/deepResearchTrace";
import type { ConversationStatus } from "@/types/agent";

interface Props {
  /** The model's opening brief — the first visible output of a gateless run. */
  brief: string | null;
  trace: ActivityItem[];
  stats: DeepStats;
  status: ConversationStatus;
  /** Separate follow-up phase signal. When "follow_up", a follow-up answer is
   *  generating and the RUN loaders must stay calm. */
  followUpStatus: "follow_up" | "follow_up_complete" | null;
}

/** Whole minutes once past a minute, seconds below that. Only ever rendered
 *  from a REAL measured span (stats.elapsedSeconds is null when no event
 *  carried a timestamp) — never an estimate. */
function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  return `${Math.round(seconds / 60)}m`;
}

function StatsRow({
  stats,
  status,
  runActive,
}: {
  stats: DeepStats;
  status: ConversationStatus;
  /** True only when the RUN itself is working — false during a follow-up so
   *  the "Working" spinner doesn't re-flash on the strip. */
  runActive: boolean;
}) {
  const isFinished = status === "FINISHED";
  return (
    <div className="flex flex-wrap items-center gap-body font-mono text-[0.78rem] text-text-muted">
      <span>
        <span className="text-text">{stats.searches}</span>
        <span className="text-text-faint">
          {stats.searches === 1 ? " search" : " searches"}
        </span>
      </span>
      <span className="text-text-faint">·</span>
      <span>
        <span className="text-text">{stats.sourcesDiscovered}</span>
        <span className="text-text-faint"> sources</span>
      </span>
      {stats.activeRound && !isFinished && (
        <>
          <span className="text-text-faint">·</span>
          <span>
            <span className="text-text-faint">round </span>
            <span className="text-text">{stats.activeRound.current}</span>
            <span className="text-text-faint">/{stats.activeRound.max}</span>
          </span>
        </>
      )}
      {stats.elapsedSeconds !== null && stats.elapsedSeconds > 0 && (
        <>
          <span className="text-text-faint">·</span>
          <span>
            <span className="text-text">{formatElapsed(stats.elapsedSeconds)}</span>
            <span className="text-text-faint"> elapsed</span>
          </span>
        </>
      )}
      {runActive && (
        <>
          <span className="text-text-faint">·</span>
          <span className="flex items-center gap-hair text-accent">
            <Loader2 className="size-3 animate-spin" aria-hidden />
            <span className="font-ui">Working</span>
          </span>
        </>
      )}
    </div>
  );
}

/** The "now" line — what the engine is doing RIGHT NOW, so a long quiet stretch
 * (a 30-60s write on a local model) reads as work, not a hang. */
function Heartbeat({
  stats,
  runActive,
}: {
  stats: DeepStats;
  /** True only during the main run — suppressed during follow-up. */
  runActive: boolean;
}) {
  if (!runActive) return null;
  let now: string | null = null;
  if (stats.activeSection) {
    now = `Writing “${stats.activeSection.title}”`;
  } else if (stats.phase === "coherence") {
    now = "Cross-checking the report for coherence…";
  } else if (stats.phase === "synthesize") {
    now = "Writing the report…";
  } else if (stats.activeSubquestion) {
    now = `Searching: “${stats.activeSubquestion.title}”`;
  }
  if (!now && !stats.lastThought) return null;
  return (
    <div className="border-b border-hairline px-body py-inline">
      {now && (
        <div className="flex items-center gap-hair font-ui text-[0.8rem] text-text">
          <Loader2 className="size-3 shrink-0 animate-spin text-accent" aria-hidden />
          <span className="truncate">{now}</span>
        </div>
      )}
      {stats.lastThought && (
        <div className="mt-hair truncate font-reading text-[0.78rem] italic text-text-muted">
          {stats.lastThought}
        </div>
      )}
    </div>
  );
}

export function DeepProgressStrip({ brief, trace, stats, status, followUpStatus }: Props) {
  const isFinished = status === "FINISHED" || status === "IDLE";
  // runActive: the research run itself is working (not a follow-up answer).
  // When followUpStatus === "follow_up", the loop re-entered for a follow-up
  // but this strip must stay calm — only the follow-up indicator (WALK-12,
  // separate lane) shows activity.
  const runActive = status === "RUNNING" && followUpStatus !== "follow_up";
  const [expandedWhenFinished, setExpandedWhenFinished] = useState(false);
  // Finished reports begin compact. A user can expand the trace explicitly;
  // active runs stay expanded regardless of the finished-view preference.
  const collapsed = isFinished && !expandedWhenFinished;

  if (collapsed) {
    return (
      <section className="rounded-card border border-hairline bg-surface-1 px-body py-inline">
        <button
          type="button"
          onClick={() => setExpandedWhenFinished(true)}
          className="group flex min-h-11 w-full items-center gap-inline text-left lg:min-h-0"
        >
          <ChevronRight className="size-3.5 text-text-faint transition-colors group-hover:text-text" aria-hidden />
          <span className="font-ui text-[0.82rem] text-text-muted transition-colors group-hover:text-text">
            How it researched
          </span>
          <span className="ml-auto">
            <StatsRow stats={stats} status={status} runActive={runActive} />
          </span>
        </button>
      </section>
    );
  }

  return (
    <section className="rounded-card border border-hairline bg-surface-1">
      <header className="flex items-center gap-inline border-b border-hairline px-body py-inline">
        {isFinished && (
          <button
            type="button"
            onClick={() => setExpandedWhenFinished(false)}
            className="grid size-11 shrink-0 place-items-center rounded-control border border-hairline text-text-faint transition-colors hover:text-text lg:size-7"
            aria-label="Collapse progress"
          >
            <ChevronDown className="size-3.5" aria-hidden />
          </button>
        )}
        <StatsRow stats={stats} status={status} runActive={runActive} />
      </header>

      {/* The heartbeat — what's happening RIGHT NOW + the engine's latest thought. */}
      <Heartbeat stats={stats} runActive={runActive} />

      {/* The brief — the model's own read of the question. First visible
          output of a gateless run, so it leads the strip's body. */}
      {brief && (
        <div className="border-b border-hairline px-body py-body" data-dr-brief="">
          <div className="mb-inline font-ui text-[0.7rem] font-semibold uppercase tracking-wide text-text-faint">
            Research brief
          </div>
          <p className="font-reading text-[0.86rem] leading-snug text-text-muted">
            {brief}
          </p>
        </div>
      )}

      {/* Live trace — the activity feed. Skipped when there's nothing yet. */}
      {trace.length > 0 && (
        <div className="px-body py-body">
          <div
            className={cn(
              "mb-inline font-ui text-[0.7rem] font-semibold uppercase tracking-wide text-text-faint",
            )}
          >
            Live trace
          </div>
          <ActivityFeed items={trace} />
        </div>
      )}
    </section>
  );
}
