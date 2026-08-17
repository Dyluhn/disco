/**
 * FollowUpStatus — small follow-up activity indicator (WALK-12 / B1).
 *
 * Shows "Reading sources…" / "Writing answer…" while a follow-up is in
 * flight so the surface doesn't look frozen.  Driven by `followUpStatus`
 * (from useDeepResearchStream, Wave 1) + the phase ActionEvents the backend
 * now emits during _follow_up_deep_research.
 *
 * Rendered BELOW the follow-up Q&A thread (before NeedMoreCard) by
 * DeepResearchSurface.  Independent of DeepProgressStrip — it never
 * re-spins the plan loader.
 */

import { Loader2 } from "lucide-react";
import type { AgentEvent } from "@/types/agent";

// ── Phase → display label map ─────────────────────────────────────────────────

function derivePhaseLabel(events: AgentEvent[]): string {
  // Walk the events backwards to find the most recent phase ActionEvent.
  // (Phase events are filtered out of deriveLiveTrace as engine internals,
  // but we read them directly here for the follow-up indicator.)
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "action" && e.tool_call?.tool_name === "phase") {
      const phase = String(
        (e.tool_call.arguments as Record<string, unknown>).phase ?? "",
      );
      if (phase === "reading") return "Reading sources…";
      if (phase === "synthesizing") return "Writing answer…";
    }
  }
  return "Generating answer…";
}

// ── Component ─────────────────────────────────────────────────────────────────

export interface FollowUpStatusProps {
  /** Separate follow-up status field from useDeepResearchStream (WALK-11).
   *  "follow_up" = generation in progress; "follow_up_complete" = done;
   *  null = no follow-up active. */
  followUpStatus: "follow_up" | "follow_up_complete" | null;
  /** Full event log — scanned for the latest phase ActionEvent. */
  events: AgentEvent[];
}

/**
 * Renders a small activity strip while a follow-up answer is being generated.
 * Returns null when no follow-up is active or after it completes (the answer
 * renders in the Q&A thread above this component).
 */
export function FollowUpStatus({ followUpStatus, events }: FollowUpStatusProps) {
  if (followUpStatus !== "follow_up") return null;

  const label = derivePhaseLabel(events);

  return (
    <div className="mx-auto w-full max-w-doc">
      <div className="flex items-center gap-inline rounded-card border border-hairline bg-surface-1 px-body py-section">
        <Loader2
          className="size-4 shrink-0 animate-spin text-accent"
          aria-hidden
        />
        <span className="font-ui text-[0.85rem] text-text-muted">{label}</span>
      </div>
    </div>
  );
}
