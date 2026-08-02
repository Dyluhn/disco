/**
 * Live header-strip stats, split out of `deepResearchTrace.ts`
 * (PKG-12-C/TS-0052). The original `deriveStats` was a two-way split (action
 * vs observation) with a four-way `tool_name` chain inside the action arm and
 * a two-way chain inside the observation arm, all mutating a 6-field
 * accumulator inline — 37 mccabe in one callable. Distributed here into one
 * handler per tool_name/kind plus a dispatcher, each owning one decision.
 */
import type { AgentEvent, ObservationEvent, ToolResult } from "@/types/agent";
import type { DeepPlanView, DeepStats } from "@/lib/deepResearchTrace";
import { computePlanProgress } from "./planProgress";

/** The mutable working state threaded through one pass over the event
 *  stream — the same 6 fields the original inline `let`s tracked. */
interface StatsAccumulator {
  activeSubq: { title: string; index: number } | null;
  activeRound: { current: number; max: number } | null;
  sourcesDiscovered: number;
  phase: string | null;
  activeSection: { title: string; index: number } | null;
  lastThought: string | null;
}

function applySearchAction(
  acc: StatsAccumulator,
  args: Record<string, unknown>,
  titleIndex: Map<string, number>,
): void {
  const subq = String(args.subquestion ?? "");
  const idx = titleIndex.get(subq);
  if (idx !== undefined) {
    acc.activeSubq = { title: subq, index: idx };
    const cur = Number(args.round);
    const max = Number(args.rounds_max);
    if (Number.isFinite(cur) && Number.isFinite(max)) {
      acc.activeRound = { current: cur, max };
    }
  }
}

function applySynthesizeAction(
  acc: StatsAccumulator,
  args: Record<string, unknown>,
  titleIndex: Map<string, number>,
): void {
  // Writing this section now — the heartbeat for the quiet stretch.
  const section = String(args.section ?? "");
  const idx = titleIndex.get(section);
  acc.activeSection = { title: section, index: idx ?? 0 };
  if (acc.activeSubq && acc.activeSubq.title === section) acc.activeSubq = null;
  acc.activeRound = null; // rounds are a gather concept
}

function applySectionDoneAction(
  acc: StatsAccumulator,
  args: Record<string, unknown>,
): void {
  const title = String(args.title ?? "");
  if (acc.activeSection && acc.activeSection.title === title) acc.activeSection = null;
  acc.lastThought = `Finished “${title}”`;
}

function applyPhaseAction(
  acc: StatsAccumulator,
  args: Record<string, unknown>,
): void {
  acc.phase = String(args.phase ?? "") || null;
}

function applyObservation(acc: StatsAccumulator, r: ToolResult): void {
  if (r.tool_name === "observation" && r.structured) {
    const added = Number(r.structured.added);
    if (Number.isFinite(added)) acc.sourcesDiscovered += added;
  } else if (r.tool_name === "gap_reason" && r.structured) {
    // The engine's between-rounds reasoning: what it found, whether it's
    // sufficient, what's still missing — the Google-style "thought" line.
    const rationale = String(r.structured.rationale ?? "").trim();
    if (rationale) acc.lastThought = rationale;
  }
}

/** Dispatches one event to its handler. Each arm owns exactly one decision;
 *  the branching that used to live inline here now lives in the handlers. */
function applyEvent(
  acc: StatsAccumulator,
  e: AgentEvent,
  titleIndex: Map<string, number>,
): void {
  if (e.kind === "action" && e.tool_call) {
    const name = e.tool_call.tool_name;
    const args = e.tool_call.arguments;
    if (name === "search") applySearchAction(acc, args, titleIndex);
    else if (name === "synthesize_section") applySynthesizeAction(acc, args, titleIndex);
    else if (name === "section_done") applySectionDoneAction(acc, args);
    else if (name === "phase") applyPhaseAction(acc, args);
  } else if (e.kind === "observation" && (e as ObservationEvent).tool_result) {
    applyObservation(acc, (e as ObservationEvent).tool_result);
  }
}

/** Live stats for the mono header strip. Computed from the event stream;
 *  cheap to re-run on every reducer update. */
export function computeStats(
  events: AgentEvent[],
  plan: DeepPlanView | null,
): DeepStats {
  const acc: StatsAccumulator = {
    activeSubq: null,
    activeRound: null,
    sourcesDiscovered: 0,
    phase: null,
    activeSection: null,
    lastThought: null,
  };
  let firstSeq = Number.POSITIVE_INFINITY;
  let lastSeq = 0;
  const titleIndex = new Map<string, number>();
  plan?.steps.forEach((s, i) => titleIndex.set(s.title, i + 1));

  for (const e of events) {
    if (e.seq != null) {
      firstSeq = Math.min(firstSeq, e.seq);
      lastSeq = Math.max(lastSeq, e.seq);
    }
    applyEvent(acc, e, titleIndex);
  }

  const subquestionsTotal = plan?.steps.length ?? 0;
  let subquestionsDone = 0;
  if (plan) {
    const progress = computePlanProgress(events, plan);
    for (const v of progress.values()) if (v === "done") subquestionsDone += 1;
  }
  // event seqs are monotonic per-conversation; we use them as a coarse "ticks"
  // proxy when no timestamps are available. The surface renders this as
  // "9 min elapsed" only if it's plausible — null otherwise.
  const elapsedSeconds =
    Number.isFinite(firstSeq) && lastSeq > firstSeq
      ? null // we don't have wall-clock timestamps on events; surface omits
      : null;

  return {
    subquestionsTotal,
    subquestionsDone,
    activeSubquestion: acc.activeSubq,
    activeRound: acc.activeRound,
    sourcesDiscovered: acc.sourcesDiscovered,
    elapsedSeconds,
    phase: acc.phase,
    activeSection: acc.activeSection,
    lastThought: acc.lastThought,
  };
}
