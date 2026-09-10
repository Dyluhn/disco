/**
 * FollowUpStatus — small follow-up activity indicator (WALK-12 / B1).
 *
 * Shows "Reading sources…" / "Answering — 145 tokens so far · 24 s" /
 * "Checking claim k of n against the sources…" while a follow-up is in
 * flight so the surface doesn't look frozen.  Driven by `followUpStatus`
 * (from useDeepResearchStream, Wave 1) + the `phase` and `model_activity`
 * ActionEvents the backend emits during _follow_up_deep_research.
 *
 * Rendered BELOW the follow-up Q&A thread (before NeedMoreCard) by
 * DeepResearchSurface.  Independent of DeepProgressStrip — it never
 * re-spins the plan loader.
 */

import { Loader2 } from "lucide-react";
import { countOf } from "@/lib/deepResearchHeartbeat";
import type { AgentEvent } from "@/types/agent";

// ── Report → display label map ────────────────────────────────────────────────

/** The newest thing the follow-up REPORTED — its `phase`, or the answer call's
 *  own `model_activity` heartbeat — or null when none has landed. (Both are
 *  filtered out of deriveLiveTrace as engine internals; the follow-up indicator
 *  reads them directly.) The NEWEST one wins outright — a report this file does
 *  not name must not let an older, already-finished one keep speaking for the
 *  follow-up.
 *
 *  `model_activity` is matched only at `stage === "follow_up"`: the run's own
 *  brief/turn/writer calls share the action name, and this strip speaks for the
 *  follow-up alone. */
function newestReport(
  events: AgentEvent[],
): { name: string; args: Record<string, unknown> } | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind !== "action" || !e.tool_call) continue;
    const { tool_name: name, arguments: args } = e.tool_call;
    if (name === "phase") return { name, args };
    if (name === "model_activity" && args.stage === "follow_up") return { name, args };
  }
  return null;
}

/** The answer call while it is still open — the leg that used to be a silent
 *  half-minute on screen (P3 measured 23–28 s to the first token). Every number
 *  is one the backend measured on the socket it delivered: tokens the provider
 *  has streamed so far, and seconds since the call was made. Nothing counts up
 *  on its own; the line changes only when the next frame lands. */
function answeringLabel(args: Record<string, unknown>): string {
  const seconds = Number(args.seconds);
  const tokens = Number(args.tokens_streamed);
  if (!Number.isFinite(seconds) || !Number.isFinite(tokens)) return "Generating answer…";
  const elapsed = `${Math.round(seconds)} s`;
  // `streams: false` is the backend saying this transport delivers the whole
  // answer at once, so this is the only frame coming and the silence after it
  // belongs to the transport. Reporting "0 tokens so far" would read as a
  // stalled model.
  return args.streams === false
    ? `Answering (this provider does not stream) · ${elapsed}`
    : `Answering — ${countOf(tokens, "token")} so far · ${elapsed}`;
}

/** The grounding pass checks the answer's claims against the report's own
 *  sources, one event per claim. `done`/`total` is the work it is spending, so
 *  the strip counts it down instead of spinning with no end in sight — this is
 *  the pass that used to run silently long enough to look like a hang. */
function groundingLabel(args: Record<string, unknown>): string {
  const done = Number(args.done);
  const total = Number(args.total);
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) {
    return "Checking the answer against the sources…";
  }
  return `Checking claim ${done} of ${total} against the sources…`;
}

function deriveLabel(events: AgentEvent[]): string {
  const report = newestReport(events);
  if (report === null) return "Generating answer…";
  if (report.name === "model_activity") return answeringLabel(report.args);
  const args = report.args;
  const phase = String(args.phase ?? "");
  if (phase === "reading") return "Reading sources…";
  if (phase === "synthesizing") return "Writing answer…";
  if (phase === "grounding") return groundingLabel(args);
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

  const label = deriveLabel(events);

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
