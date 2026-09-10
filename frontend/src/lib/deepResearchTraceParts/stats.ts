/**
 * Live header-strip stats, split out of `deepResearchTrace.ts`
 * (PKG-12-C/TS-0052). v2 (gateless deep research): there is no plan and no
 * checklist — progress derives from the search / observation /
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
  refusals: number;
  sectionsDone: number;
  sourcesDiscovered: number;
  phase: string | null;
  activeSection: { title: string } | null;
  lastThought: string | null;
  lastThoughtLeg: "research" | "writer" | null;
  /** Passages on the newest checkpoint that has not been resumed past yet. */
  pendingCarry: number | null;
  carriedSources: number | null;
  firstTs: number;
  lastTs: number;
}

/**
 * A resume carries the POOL, not the turn budget.
 *
 * The engine builds a fresh `_AgentState` for the continuation
 * (`agent.py:1410`) whose `turns_charged` starts at zero
 * (`_agent_state.py:68`), and every `turn` payload is derived from that
 * counter (`agent.py:1316-1317` → `_progress_events.py:98`), so a resumed run
 * genuinely opens at "Turn 1 of N". Its evidence pool is not fresh: the
 * checkpoint's passages are rebuilt (`execute.py:274`) and admitted exempt
 * from the new source budget (`agent.py:1413`). Only saying the first half is
 * what made L29's `97-resumed-no-checkpoint.png` read as a miscount — "Turn 1
 * of 8" over nine sources nobody had searched for yet.
 */
function applyResumeCarry(acc: StatsAccumulator, e: AgentEvent): void {
  if (e.kind === "research_checkpoint") {
    acc.pendingCarry = e.passages.length;
  } else if (e.kind === "status" && e.status === "RUNNING" && acc.pendingCarry !== null) {
    acc.carriedSources = acc.pendingCarry;
    acc.pendingCarry = null;
  }
}

function applySearchAction(acc: StatsAccumulator): void {
  acc.searches += 1;
}

function applyQueryRefusedAction(acc: StatsAccumulator): void {
  acc.refusals += 1;
}

function applySynthesizeAction(
  acc: StatsAccumulator,
  args: Record<string, unknown>,
): void {
  // Pre-v2 replays: a per-section write heartbeat (v2 writes the report in
  // one pass and emits only section_done checkpoints at the end).
  acc.activeSection = { title: String(args.section ?? "") };
}

function applySectionDoneAction(
  acc: StatsAccumulator,
  args: Record<string, unknown>,
): void {
  const title = String(args.title ?? "");
  acc.sectionsDone += 1;
  if (acc.activeSection && acc.activeSection.title === title) acc.activeSection = null;
  acc.lastThought = `Finished “${title}”`;
  acc.lastThoughtLeg = "writer";
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
    // Pre-v2 replays: the engine's between-rounds reasoning — what it found,
    // whether it's sufficient, what's still missing.
    const rationale = String(r.structured.rationale ?? "").trim();
    if (rationale) {
      acc.lastThought = rationale;
      acc.lastThoughtLeg = "research";
    }
  }
}

/** Dispatches one event to its handler. Each arm owns exactly one decision;
 *  the branching that used to live inline here now lives in the handlers. */
function applyEvent(acc: StatsAccumulator, e: AgentEvent): void {
  if (e.kind === "action" && e.tool_call) {
    const name = e.tool_call.tool_name;
    const args = e.tool_call.arguments;
    if (name === "search") applySearchAction(acc);
    else if (name === "query_refused") applyQueryRefusedAction(acc);
    else if (name === "synthesize_section") applySynthesizeAction(acc, args);
    else if (name === "section_done") applySectionDoneAction(acc, args);
    else if (name === "phase") applyPhaseAction(acc, args);
  } else if (e.kind === "observation" && (e as ObservationEvent).tool_result) {
    applyObservation(acc, (e as ObservationEvent).tool_result);
  } else {
    applyResumeCarry(acc, e);
  }
}

/** Live stats for the mono header strip. Computed from the event stream;
 *  cheap to re-run on every reducer update. */
export function computeStats(events: AgentEvent[]): DeepStats {
  const acc: StatsAccumulator = {
    searches: 0,
    refusals: 0,
    sectionsDone: 0,
    sourcesDiscovered: 0,
    phase: null,
    activeSection: null,
    lastThought: null,
    lastThoughtLeg: null,
    pendingCarry: null,
    carriedSources: null,
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
    refusals: acc.refusals,
    sectionsDone: acc.sectionsDone,
    sourcesDiscovered: acc.sourcesDiscovered,
    elapsedSeconds,
    phase: acc.phase,
    activeSection: acc.activeSection,
    lastThought: acc.lastThought,
    lastThoughtLeg: acc.lastThoughtLeg,
    carriedSources: acc.carriedSources,
  };
}
