/**
 * W-43 — AgentStageCard: the collapsed view of the agent's progress shown on the
 * control pane when "Verbose Agent Chat" is OFF (and once the first plan is approved).
 *
 * It derives a single glanceable STAGE from the existing `deriveLiveSignal`
 * (planning / reading / executing / building / stalled / done / idle) instead of the
 * full step-by-step feed — quieter for users who don't want the running narrative.
 *
 * codex P1 (artifact preservation): this is NOT a blunt hide. The card is
 * click-to-expand — expanding reveals the FULL ActivityFeed inline (every inline
 * artifact / download / screenshot / deck-export affordance it carries). The same
 * full feed is also always reachable from the inspector's "Agent History" tab, and
 * the deliverable download/open panel + the confirm/decision/question gates render
 * outside this card (in BuildSurface), so collapsing never strands an affordance.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { deriveStage, type AgentStage } from "@/components/build/agentStage";
import type { ActivityItem } from "@/lib/buildTrace";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

const STAGE_LABEL: Record<AgentStage, string> = {
  planning: "Planning",
  reading: "Reading",
  executing: "Executing",
  building: "Building",
  stalled: "Stalled",
  done: "Done",
  idle: "Idle",
};

const ACTIVE_STAGES = new Set<AgentStage>(["planning", "reading", "executing", "building"]);

/** UI-14 — how long the run took, from the first to the last event that carried
 * a timestamp. Returns null when the stream has no usable timestamps (replayed
 * fixtures, very short runs) so the summary just drops the clause instead of
 * printing "0 min". */
function runDuration(events: AgentEvent[]): string | null {
  const stamps = events
    .map((e) => (e.timestamp ? Date.parse(e.timestamp) : NaN))
    .filter((t) => Number.isFinite(t));
  if (stamps.length < 2) return null;
  const ms = Math.max(...stamps) - Math.min(...stamps);
  if (ms <= 0) return null;
  const mins = Math.round(ms / 60_000);
  if (mins < 1) return `${Math.max(1, Math.round(ms / 1000))} sec`;
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

/** UI-14 — the finished run's one-line receipt. After FINISHED the collapsed
 * card used to say only "Verbose chat is off", which under the (now fixed)
 * squeezed Activity section read as an empty box. A settled run gets a real
 * summary — how long it took, how many steps it ran — with the expand
 * affordance spelled out rather than implied. */
function runSummaryLine(
  events: AgentEvent[],
  activity: ActivityItem[],
  expanded: boolean,
): string {
  const steps = activity.filter((a) => a.kind === "action").length;
  const took = runDuration(events);
  const parts = [
    took ? `Finished in ${took}` : "Finished",
    `${steps} ${steps === 1 ? "step" : "steps"}`,
    expanded ? "collapse history" : "expand history",
  ];
  return parts.join(" · ");
}

export function AgentStageCard({
  events,
  status,
  activity,
  conversationId,
  surface = "build",
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  /** The full derived activity — rendered inline when the card is expanded. */
  activity: ActivityItem[];
  conversationId?: string;
  surface?: "build" | "agent";
}) {
  const [expanded, setExpanded] = useState(false);
  const stage = deriveStage(events, status);
  const active = ACTIVE_STAGES.has(stage);

  return (
    <div
      data-testid="agent-stage-card"
      data-stage={stage}
      className={cn(
        "flex flex-col rounded-card border bg-surface-1",
        stage === "stalled" ? "border-warn/40" : "border-hairline",
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        aria-label={expanded ? "Collapse agent history" : "Expand agent history"}
        data-disco-control="build.stage-card-toggle"
        className="flex min-h-11 items-center gap-inline px-body py-inline text-left transition-colors hover:bg-surface-2/60 lg:min-h-0"
      >
        {expanded ? (
          <ChevronDown className="size-3.5 shrink-0 text-text-faint" aria-hidden />
        ) : (
          <ChevronRight className="size-3.5 shrink-0 text-text-faint" aria-hidden />
        )}
        <span
          className={cn(
            "flex size-2 shrink-0 items-center justify-center",
            active && "animate-pulse",
          )}
          aria-hidden
        >
          {active ? (
            <Loader2 className="size-3.5 animate-spin text-accent" aria-hidden />
          ) : (
            <span
              className={cn(
                "size-1.5 rounded-full",
                stage === "stalled" ? "bg-warn" : stage === "done" ? "bg-supported" : "bg-text-faint",
              )}
            />
          )}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block font-ui text-[0.9rem] font-medium text-text">
            {surface === "agent" && stage === "building" ? "Working" : STAGE_LABEL[stage]}
          </span>
          <span className="block font-ui text-[0.74rem] text-text-faint">
            {stage === "done"
              ? runSummaryLine(events, activity, expanded)
              : `Verbose chat is off — ${expanded ? "click to collapse" : "click to expand"} the full history`}
          </span>
        </span>
      </button>
      {expanded && (
        <div className="border-t border-hairline px-body py-inline">
          <ActivityFeed items={activity} conversationId={conversationId} />
        </div>
      )}
    </div>
  );
}
