/**
 * DeepProgressStrip — the live-progress treatment during a Deep Research
 * run. Three components inside the strip:
 *
 *   ┌─────────────────────────────────────────────────────────────┐
 *   │ Sub-questions  3 of 6  ·  30 sources  ·  Round 2/4   [ ▾ ]  │  ← collapsible header
 *   ├─────────────────────────────────────────────────────────────┤
 *   │ ☑ 1. What products are shipping today?            done      │
 *   │ ⟳ 2. What are the manufacturing bottlenecks?      active    │  ← PlanPanel
 *   │ ◌ 3. How do costs compare to lithium-ion?         pending   │     (read-only)
 *   ├─────────────────────────────────────────────────────────────┤
 *   │ Searching: "SK On pilot production timeline"                │
 *   │   Round 2: +6 sources (12 total for this sub-question)      │  ← ActivityFeed
 *   │ Coverage sufficient                                          │     (the live trace)
 *   └─────────────────────────────────────────────────────────────┘
 *
 * When the run finishes the strip collapses to a single line: "How it
 * researched · 3 of 6 sub-questions covered · 30 sources · [ ▸ Expand ]".
 * Click to re-open the full trace (transparency preserved, not discarded).
 */

import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { useState } from "react";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { PlanPanel } from "@/components/build/PlanPanel";
import type { PlanView, StepState } from "@/lib/buildTrace";
import { cn } from "@/lib/cn";
import type { ActivityItem } from "@/lib/buildTrace";
import type { DeepPlanView, DeepStats } from "@/lib/deepResearchTrace";
import type { ConversationStatus } from "@/types/agent";

interface Props {
  plan: DeepPlanView | null;
  progress: Map<number, StepState>;
  trace: ActivityItem[];
  stats: DeepStats;
  status: ConversationStatus;
}

function StatsRow({ stats, status }: { stats: DeepStats; status: ConversationStatus }) {
  const isFinished = status === "FINISHED";
  return (
    <div className="flex flex-wrap items-center gap-body font-mono text-[0.78rem] text-text-muted">
      <span>
        <span className="text-text">{stats.subquestionsDone}</span>
        <span className="text-text-faint"> of </span>
        <span className="text-text">{stats.subquestionsTotal}</span>
        <span className="text-text-faint"> sub-questions</span>
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
      {status === "RUNNING" && (
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

export function DeepProgressStrip({ plan, progress, trace, stats, status }: Props) {
  const isFinished = status === "FINISHED" || status === "IDLE";
  const [collapsedManually, setCollapsedManually] = useState(false);
  // when finished, default collapsed unless the user expands; while running,
  // always expanded.
  const collapsed = isFinished && collapsedManually;

  // Cast our DeepPlanView shape to Build's PlanView (structurally compatible:
  // both have id/summary/steps/revision/context fields). The PlanPanel just
  // reads steps + progress; no Build-specific assumptions.
  const planForPanel: PlanView | null = plan
    ? {
        id: plan.id,
        summary: plan.summary,
        steps: plan.steps,
        revision: plan.revision,
        context: plan.context,
      }
    : null;

  if (collapsed) {
    return (
      <section className="rounded-card border border-hairline bg-surface-1 px-body py-inline">
        <button
          type="button"
          onClick={() => setCollapsedManually(false)}
          className="group flex w-full items-center gap-inline text-left"
        >
          <ChevronRight className="size-3.5 text-text-faint transition-colors group-hover:text-text" aria-hidden />
          <span className="font-ui text-[0.82rem] text-text-muted transition-colors group-hover:text-text">
            How it researched
          </span>
          <span className="ml-auto">
            <StatsRow stats={stats} status={status} />
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
            onClick={() => setCollapsedManually(true)}
            className="grid size-7 place-items-center rounded-control border border-hairline text-text-faint transition-colors hover:text-text"
            aria-label="Collapse progress"
          >
            <ChevronDown className="size-3.5" aria-hidden />
          </button>
        )}
        <StatsRow stats={stats} status={status} />
      </header>

      {/* Plan checklist — reuses Build's PlanPanel in read-only mode. */}
      {planForPanel && (
        <div className="border-b border-hairline px-body py-body">
          <PlanPanel plan={planForPanel} progress={progress} />
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
