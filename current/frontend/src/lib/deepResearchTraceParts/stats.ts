/**
 * Live header-strip stats, split out of `deepResearchTrace.ts`
 * (PKG-12-C/TS-0052). v2 (gateless deep research): there is no plan and no
 * per-sub-question checklist — progress derives from the search / observation /
 * phase / section_done events the engine actually emits. Elapsed time is REAL:
 * the span between the first and the latest event timestamps
 * (BaseEvent.timestamp), no longer a hardcoded null. Pre-v2 vocabularies
 * (synthesize_section actions, gap_reason observations) keep their handlers so
 * replayed historical conversations still render honestly. The shape stays a
 * dispatcher plus one small handler per tool_name/kind (mccabe budget).
 */
import type { AgentEvent, ObservationEvent, ToolResult } from "@/types/agent";
import type { DeepStats } from "@/lib/deepResearchTrace";

/** The mutable working state threaded through one pass over the event
 *  stream — the DeepStats fields plus the timestamp extrema. */
interface StatsAccumulator {
  searches: number;
  sectionsDone: number;
  activeSubq: { title: string } | null;
  activeRound: { current: number; max: number } | null;
  sourcesDiscovered: number;
  phase: string | null;
  activeSection: { title: string } | null;
  lastThought: string | null;
  firstTs: number;
  lastTs: number;
}

function applySearchAction(
  acc: StatsAccumulator,
  args: Record<string, unknown>,
): void {
  acc.searches += 1;
  const subq = String(args.subquestion ?? "");
  if (subq) acc.activeSubq = { title: subq };
  const cur = Number(args.round);
  const max = Number(args.rounds_max);
  if (Number.isFinite(cur) && Number.isFinite(max)) {
    acc.activeRound = { current: cur, max };
  }
}

function applySynthesizeAction(
  acc: StatsAccumulator,
  args: Record<string, unknown>,
): void {
  // Pre-v2 replays: a per-section write heartbeat (v2 writes the report in
  // one pass and emits only section_done checkpoints at the end).
  acc.activeSection = { title: String(args.section ?? "") };
  acc.activeSubq = null;
  acc.activeRound = null; // rounds are a gather concept
}

function applySectionDoneAction(
  acc: StatsAccumulator,
  args: Record<string, unknown>,
): void {
  const title = String(args.title ?? "");
  acc.sectionsDone += 1;
  if (acc.activeSection && acc.activeSection.title === title) acc.activeSection = null;
  acc.lastThought = `Finished “${title}”`;
}

function applyPhaseAction(
  acc: StatsAccumulator,
  args: Record<string, unknown>,
): void {
  acc.phase = String(args.phase ?? "") || null;
  if (acc.phase && acc.phase !== "gather") {
    // Gather is over — a "Searching …" indicator would now be stale.
    acc.activeSubq = null;
    acc.activeRound = null;
  }
}

function applyObservation(acc: StatsAccumulator, r: ToolResult): void {
  if (r.tool_name === "observation" && r.structured) {
    const added = Number(r.structured.added);
    if (Number.isFinite(added)) acc.sourcesDiscovered += added;
  } else if (r.tool_name === "gap_reason" && r.structured) {
    // Pre-v2 replays: the engine's between-rounds reasoning — what it found,
    // whether it's sufficient, what's still missing.
    const rationale = String(r.structured.rationale ?? "").trim();
    if (rationale) acc.lastThought = rationale;
  }
}

/** Dispatches one event to its handler. Each arm owns exactly one decision;
 *  the branching that used to live inline here now lives in the handlers. */
function applyEvent(acc: StatsAccumulator, e: AgentEvent): void {
  if (e.kind === "action" && e.tool_call) {
    const name = e.tool_call.tool_name;
    const args = e.tool_call.arguments;
    if (name === "search") applySearchAction(acc, args);
    else if (name === "synthesize_section") applySynthesizeAction(acc, args);
    else if (name === "section_done") applySectionDoneAction(acc, args);
    else if (name === "phase") applyPhaseAction(acc, args);
  } else if (e.kind === "observation" && (e as ObservationEvent).tool_result) {
    applyObservation(acc, (e as ObservationEvent).tool_result);
  }
}

/** Live stats for the mono header strip. Computed from the event stream;
 *  cheap to re-run on every reducer update. */
export function computeStats(events: AgentEvent[]): DeepStats {
  const acc: StatsAccumulator = {
    searches: 0,
    sectionsDone: 0,
    activeSubq: null,
    activeRound: null,
    sourcesDiscovered: 0,
    phase: null,
    activeSection: null,
    lastThought: null,
    firstTs: Number.POSITIVE_INFINITY,
    lastTs: Number.NEGATIVE_INFINITY,
  };

  for (const e of events) {
    const ts = e.timestamp ? Date.parse(e.timestamp) : Number.NaN;
    if (Number.isFinite(ts)) {
      acc.firstTs = Math.min(acc.firstTs, ts);
      acc.lastTs = Math.max(acc.lastTs, ts);
    }
    applyEvent(acc, e);
  }

  // Real wall-clock span between the first and latest event timestamps —
  // events carry ISO-8601 timestamps (event-state contract §2.1), so this is
  // measured, never fabricated. Null only when no event carried a timestamp.
  const elapsedSeconds =
    acc.lastTs >= acc.firstTs
      ? Math.round((acc.lastTs - acc.firstTs) / 1000)
      : null;

  return {
    searches: acc.searches,
    sectionsDone: acc.sectionsDone,
    activeSubquestion: acc.activeSubq,
    activeRound: acc.activeRound,
    sourcesDiscovered: acc.sourcesDiscovered,
    elapsedSeconds,
    phase: acc.phase,
    activeSection: acc.activeSection,
    lastThought: acc.lastThought,
  };
}
