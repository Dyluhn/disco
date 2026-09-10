/**
 * DeepProgressStrip — the live-progress treatment during a Deep Research run.
 *
 *   ┌──────────────────────────────────────────────────────────────────┐
 *   │ 12 searches · 30 sources · Turn 7 of 16 · 4m elapsed · ● Live    │  ← StatsRow
 *   ├──────────────────────────────────────────────────────────────────┤
 *   │ Searching: "SK On pilot production timeline" · 8 s               │  ← Heartbeat:
 *   │ Turn 7 of 16 · searching                                         │    the newest
 *   │ on "pilot line capacity in Georgia"                               │    reported fact
 *   │ found X, still missing Y                                          │    + its age
 *   ├──────────────────────────────────────────────────────────────────┤
 *   │ Research brief — how the model read the question                 │  ← the brief
 *   ├──────────────────────────────────────────────────────────────────┤
 *   │ Searching: "SK On pilot production timeline"                     │
 *   │   Added 6 sources (12 admitted so far)                           │  ← ActivityFeed
 *   │ Framed the question                                              │    (the live trace)
 *   └──────────────────────────────────────────────────────────────────┘
 *
 * Every line above is the latest EVENT of its kind plus its age. Nothing is
 * inferred from `status`: the old "⟳ Working" spinner span on `RUNNING` alone
 * and the heartbeat showed a phase label emitted once before the loop started,
 * so a run that had said nothing for ten minutes looked identical to one
 * mid-search. `ActivitySignal` now ages off the newest event instead (Live /
 * quiet for 35 s / no signal for 1:12), and when the loop is holding on the
 * search pool the heartbeat carries the hold's own live countdown. Past a full
 * minute of silence the heartbeat also attaches the next action (Stop, and what
 * it keeps) — a state with no way out is a wall, not a status.
 *
 * When the run finishes the strip collapses to a single line: "How it
 * researched · 12 searches · 30 sources · [ ▸ Expand ]". Click to re-open the
 * full trace (transparency preserved, not discarded).
 */

import { ChevronDown, ChevronRight, WifiOff } from "lucide-react";
import { useState } from "react";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { useNowMs } from "@/hooks/useNowMs";
import { cn } from "@/lib/cn";
import type { ActivityItem } from "@/lib/buildTrace";
import {
  formatDuration,
  heartbeatReading,
  modelIsBuffered,
  researchPhaseLabel,
  signalReading,
  writerLegLeads,
} from "@/lib/deepResearchHeartbeat";
import type { ActiveHold, DeepActivity, DeepStats } from "@/lib/deepResearchTrace";
import type { ConversationStatus } from "@/types/agent";
import { ActivitySignal } from "./ActivitySignal";
import { DeepResearchHoldLine } from "./DeepResearchHoldPanel";

interface Props {
  /** The model's opening brief — the first visible output of a gateless run. */
  brief: string | null;
  trace: ActivityItem[];
  stats: DeepStats;
  status: ConversationStatus;
  /** The latest reported fact of each kind, with the instant it was reported. */
  activity: DeepActivity;
  /** Separate follow-up phase signal. When "follow_up", a follow-up answer is
   *  generating and the RUN loaders must stay calm. */
  followUpStatus: "follow_up" | "follow_up_complete" | null;
  /** Set once the user has chosen "Continue waiting" on an active hold — the
   *  decision panel collapses into the heartbeat's one-line form. */
  collapsedHold?: ActiveHold | null;
  /** Stop from the collapsed hold line (the same cancel the top bar sends). */
  onStopHold?: () => void;
  /** The live socket's own state. "degraded" replaces the Live dot with the
   *  same reconnecting pill the Build surface shows — a stream that is gone
   *  cannot report that the run is live (UI-33). */
  connectionState?: "connected" | "degraded";
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
  activity,
  nowMs,
  connectionState,
}: {
  stats: DeepStats;
  status: ConversationStatus;
  /** True only when the RUN itself is working — false during a follow-up so
   *  the activity signal doesn't re-flash on the strip. */
  runActive: boolean;
  activity: DeepActivity;
  nowMs: number;
  connectionState: "connected" | "degraded";
}) {
  const isFinished = status === "FINISHED";
  // The turn counter is the budget the model itself is spending, so it beats a
  // phase label that the engine emits once. The label stays as the fallback for
  // runs (and replays) that carry no `turn` events.
  const turn = activity.turn;
  const progress = turn
    ? `Turn ${turn.payload.n} of ${turn.payload.of}`
    : stats.phase
      ? researchPhaseLabel(stats.phase)
      : null;
  return (
    <div className="flex flex-wrap items-center gap-body font-mono text-[0.78rem] text-text-muted">
      <span>
        <span className="text-text">{stats.searches}</span>
        <span className="text-text-faint">
          {stats.searches === 1 ? " search" : " searches"}
        </span>
      </span>
      {/* Queries the host refused before they reached an engine. Named beside
          the searches because a turn can propose three and fire none: without
          this the strip's "0 searches" is the only thing a reader sees while
          the run is working normally. */}
      {stats.refusals > 0 && (
        <>
          <span className="text-text-faint">·</span>
          <span data-dr-refusals="">
            <span className="text-text">{stats.refusals}</span>
            <span className="text-text-faint"> not searched</span>
          </span>
        </>
      )}
      <span className="text-text-faint">·</span>
      <span>
        <span className="text-text">{stats.sourcesDiscovered}</span>
        <span className="text-text-faint"> sources</span>
      </span>
      {/* A resumed run gets its OWN full turn budget but keeps the pool it
          stopped with, so the turn counter honestly restarts at 1 over sources
          nobody has searched for yet. Naming the carried part is the only thing
          that makes both numbers readable at once. */}
      {stats.carriedSources !== null && (
        <>
          <span className="text-text-faint">·</span>
          <span data-dr-carried="">
            <span className="text-text">{stats.carriedSources}</span>
            <span className="text-text-faint"> retained from earlier research</span>
          </span>
        </>
      )}
      {progress && !isFinished && (
        <>
          <span className="text-text-faint">·</span>
          <span className="text-text">{progress}</span>
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
      {/* The stream is gone, so nothing on this row can speak for the run.
          Same pill, same words as the Build surface's `build.connection`. */}
      {connectionState === "degraded" && (
        <>
          <span className="text-text-faint">·</span>
          <span
            title="Stream reconnecting — activity will replay when the connection returns"
            data-disco-control="dr.connection"
            data-connection-state="degraded"
            className="flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.7rem] text-text-muted"
          >
            <WifiOff className="size-3" aria-hidden />
            reconnecting…
          </span>
        </>
      )}
      {/* No event has a timestamp → no honest age exists → no indicator. */}
      {connectionState === "connected" && runActive && activity.lastEventAt !== null && (
        <>
          <span className="text-text-faint">·</span>
          <ActivitySignal
            signal={signalReading(
              activity.lastEventAt,
              nowMs,
              activity.lastEventAt,
              // The heartbeat line beside this chip already reads the same fact
              // off the same event. Without it the chip warned "no signal for
              // 2:00" next to "Waiting for model reply (this driver does not report
              // progress while it works) · 2:00".
              modelIsBuffered(activity),
            )}
            className="text-[0.78rem]"
          />
        </>
      )}
    </div>
  );
}

/**
 * Past a full minute of silence the indicator chip alone isn't enough: the
 * state needs a next action attached to it. This says where the run stands and
 * what the user can do about it, and appears nowhere else — during a hold the
 * hold line already owns that job, and on a buffered driver so does its
 * "does not report progress while it works".
 */
function QuietNote({
  activity,
  nowMs,
  holdLine,
}: {
  activity: DeepActivity;
  nowMs: number;
  /** The collapsed hold line, when one is up and already explains the quiet. */
  holdLine: ActiveHold | null;
}) {
  if (holdLine !== null || modelIsBuffered(activity)) return null;
  const lastEventAt = activity.lastEventAt;
  if (lastEventAt === null) return null;
  const signal = signalReading(lastEventAt, nowMs, lastEventAt);
  if (signal.level !== "silent") return null;
  return (
    <div className="mt-hair font-ui text-[0.78rem] leading-snug text-warn">
      Nothing reported for {formatDuration(signal.ageSeconds)}. The run may still be
      working — Stop ends it and keeps the sources it has.
    </div>
  );
}

/**
 * The engine's latest thought, unless it describes searching while a writer-leg
 * fact leads.
 *
 * Same rule the heartbeat reading applies to the turn line and the subquestion:
 * once the writer leads, nothing on the strip may still describe searching. A
 * gap rationale is research-leg reasoning; a finished section is the writer's
 * own and stays.
 */
function legSafeThought(stats: DeepStats, activity: DeepActivity): string | null {
  if (stats.lastThoughtLeg === "research" && writerLegLeads(activity)) return null;
  return stats.lastThought;
}

/** The "now" line — what the engine last REPORTED and how long ago, so a long
 *  quiet stretch reads as what it is instead of as either work or a hang. */
function Heartbeat({
  activity,
  nowMs,
  runActive,
  stats,
  collapsedHold,
  onStopHold,
}: {
  activity: DeepActivity;
  nowMs: number;
  /** True only during the main run — suppressed during follow-up. */
  runActive: boolean;
  stats: DeepStats;
  collapsedHold: ActiveHold | null;
  onStopHold?: () => void;
}) {
  if (!runActive) return null;
  const holdLine = collapsedHold && onStopHold ? collapsedHold : null;
  const reading = heartbeatReading(activity, nowMs);
  // Pre-v2 replays have no `turn`/`model_activity` events; their per-section
  // write heartbeat is the only "now" they carry, so it fills in when the v2
  // vocabulary says nothing. It can never outrank a real v2 event.
  const now =
    reading.now ?? (stats.activeSection ? `Writing “${stats.activeSection.title}”` : null);
  const lastThought = legSafeThought(stats, activity);
  if (!holdLine && !now && !lastThought) return null;
  return (
    <div className="border-b border-hairline px-body py-inline">
      {holdLine && onStopHold ? (
        <DeepResearchHoldLine hold={holdLine} nowMs={nowMs} onStop={onStopHold} />
      ) : (
        now && <div className="truncate font-ui text-[0.8rem] text-text">{now}</div>
      )}
      {reading.turn && (
        <div className="mt-hair truncate font-mono text-[0.75rem] text-text-faint">
          {reading.turn}
        </div>
      )}
      {reading.subquestion && (
        <div className="mt-hair truncate font-reading text-[0.78rem] italic text-text-muted">
          on “{reading.subquestion}”
        </div>
      )}
      {lastThought && (
        <div className="mt-hair truncate font-reading text-[0.78rem] italic text-text-muted">
          {lastThought}
        </div>
      )}
      <QuietNote activity={activity} nowMs={nowMs} holdLine={holdLine} />
    </div>
  );
}

export function DeepProgressStrip({
  brief,
  trace,
  stats,
  status,
  activity,
  followUpStatus,
  collapsedHold = null,
  onStopHold,
  connectionState = "connected",
}: Props) {
  const isFinished = status === "FINISHED" || status === "IDLE";
  // runActive: the research run itself is working (not a follow-up answer).
  // When followUpStatus === "follow_up", the loop re-entered for a follow-up
  // but this strip must stay calm — only the follow-up indicator (WALK-12,
  // separate lane) shows activity.
  const runActive = status === "RUNNING" && followUpStatus !== "follow_up";
  // The ages and countdowns have to move while nothing arrives — that is the
  // whole point. Only while the run is live; a finished report holds no timer.
  const nowMs = useNowMs(runActive);
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
            <StatsRow
              stats={stats}
              status={status}
              runActive={runActive}
              activity={activity}
              nowMs={nowMs}
              connectionState={connectionState}
            />
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
        <StatsRow
          stats={stats}
          status={status}
          runActive={runActive}
          activity={activity}
          nowMs={nowMs}
          connectionState={connectionState}
        />
      </header>

      {/* The heartbeat — the newest thing the engine REPORTED, plus its age. */}
      <Heartbeat
        activity={activity}
        nowMs={nowMs}
        runActive={runActive}
        stats={stats}
        collapsedHold={collapsedHold}
        onStopHold={onStopHold}
      />

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
