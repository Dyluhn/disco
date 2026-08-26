/**
 * DeepProgressStrip — the live-progress treatment during a Deep Research
 * run. Three components inside the strip:
 *
 *   ┌─────────────────────────────────────────────────────────────┐
 *   │ 12 searches  ·  30 sources  ·  Writing report  ·  4m       [ ▾ ]  │
 *   ├─────────────────────────────────────────────────────────────┤
 *   │ Searching: "SK On pilot production timeline"                │  ← heartbeat
 *   ├─────────────────────────────────────────────────────────────┤
 *   │ The brief: how the model read the question and the angles   │  ← the brief
 *   │ it intends to chase.                                        │
 *   ├─────────────────────────────────────────────────────────────┤
 *   │ Searching: "SK On pilot production timeline"                │
 *   │   Added 6 sources (12 admitted so far)                       │  ← ActivityFeed
 *   │ Framed the question                                          │     (the live trace)
 *   └─────────────────────────────────────────────────────────────┘
 *
 * v2 (gateless deep research): the strip shows the model's own brief, actual
 * event counts, and the engine's current phase. It does not invent a planned
 * denominator for work the engine never promised.
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
      {stats.phase && !isFinished && (
        <>
          <span className="text-text-faint">·</span>
          <span className="text-text">{phaseLabel(stats.phase)}</span>
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
  } else if (stats.phase) {
    now = phaseLabel(stats.phase);
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

function phaseLabel(phase: string): string {
  const p = phase.toLowerCase();
  if (p === "gather" || p === "search" || p === "reading") return "Searching sources";
  if (p === "synthesize" || p === "synthesizing" || p === "write" || p === "writing") {
    return "Writing report";
  }
  if (p === "coherence" || p === "review" || p === "reviewing" || p === "verification") {
    return "Reviewing report";
  }
  return `Research phase: ${phase}`;
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
