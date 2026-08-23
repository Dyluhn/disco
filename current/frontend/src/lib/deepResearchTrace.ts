/**
 * Deep Research derivers — pure functions that turn the conversation's event
 * stream into the views the surface renders. Mirrors `buildTrace.ts` in
 * shape (deriveActivity / derivePlan / derivePlanProgress) but specialized
 * for the engine's tool names and the ReportEvent shape.
 *
 * The engine emits ActionEvents with tool names:
 *   "phase"               — phase transitions (gather / synthesize / coherence)
 *   "search"              — one round of retrieval for a sub-question
 *   "synthesize_section"  — synthesis of one section's body
 * And ObservationEvents with tool_name:
 *   "observation"         — result of a search round (added, total_for_subq, …)
 *   "gap_reason"          — the gap-reasoner's verdict + follow-up
 * The final ReportEvent is the assembled report.
 *
 * Module-size and complexity decomposition (PKG-12-C/TS-0051..TS-0053): the
 * three highest-mccabe derivers' implementations moved to
 * `./deepResearchTraceParts/*` so each callable stays under the architecture
 * caps — `deriveStats` alone went from 37 branches to a dispatcher plus one
 * small handler per tool_name.
 *
 * The PUBLIC DECLARATIONS deliberately stay HERE rather than becoming a
 * barrel of `export … from` re-exports — see `buildTrace.ts`'s header for why
 * (a re-export reads to the scanner as deleting the original declaration).
 * Frontend function signatures are digested WITHOUT the body, so a thin
 * delegator keeps each digest byte-identical. The types are declared here and
 * imported back by the parts (parent↔parts imports are campaign-normal);
 * type imports are erased at compile time, so that cycle never exists at
 * runtime.
 */

import type { ActivityItem } from "@/lib/buildTrace";
import { sourceUrlKey } from "@/lib/sources";
import { computeLiveTrace } from "./deepResearchTraceParts/liveTrace";
import { computePlanProgress } from "./deepResearchTraceParts/planProgress";
import { computeStats } from "./deepResearchTraceParts/stats";
import type {
  AgentEvent,
  ConversationStatus,
  PlanStep,
  ReportEvent,
  ReportSection,
} from "@/types/agent";

export type StepState = "pending" | "active" | "done";

export interface DeepPlanView {
  id: string;
  summary: string;
  steps: PlanStep[];
  revision: number;
  context: string;
}

/** The latest proposed plan (highest revision wins — same convention as
 * buildTrace.derivePlan). Returns null before any plan exists. */
export function derivePlan(events: AgentEvent[]): DeepPlanView | null {
  let latest: DeepPlanView | null = null;
  for (const e of events) {
    if (e.kind !== "plan") continue;
    if (latest === null || e.revision >= latest.revision) {
      latest = {
        id: e.id,
        summary: e.summary,
        steps: e.steps,
        revision: e.revision,
        context: e.context ?? "",
      };
    }
  }
  return latest;
}

/** Per-sub-question progress derived from the engine's emitted ActionEvents.
 *  A sub-question is "done" once `synthesize_section` has fired for it,
 *  "active" once any `search` action targeted it, "pending" otherwise.
 *  Returns a 1-based Map matching PlanPanel's contract. */
export function derivePlanProgress(
  events: AgentEvent[],
  plan: DeepPlanView | null,
): Map<number, StepState> {
  return computePlanProgress(events, plan);
}

export interface DeepStats {
  /** Sub-questions on the plan. */
  subquestionsTotal: number;
  /** Sub-questions marked done (synthesize_section fired). */
  subquestionsDone: number;
  /** Sub-question currently being worked on (title, 1-based index), or null. */
  activeSubquestion: { title: string; index: number } | null;
  /** Current round within the active sub-question, and the round cap. */
  activeRound: { current: number; max: number } | null;
  /** Sources discovered so far — sum of observations' `added` counts (the
   *  engine's per-round addition count from the gather loop). */
  sourcesDiscovered: number;
  /** Wall-clock seconds since the first event (for the live stats display). */
  elapsedSeconds: number | null;
  /** The engine phase from the latest `phase` action (gather/synthesize/coherence). */
  phase: string | null;
  /** Section currently being WRITTEN (synthesize_section fired, section_done not
   *  yet) — the heartbeat for the long quiet synthesis stretch on local models. */
  activeSection: { title: string; index: number } | null;
  /** The engine's latest "thought" — a gap_reason rationale ("found X, still
   *  missing Y") or a section completion — surfaced so a working run never
   *  reads as a hang. */
  lastThought: string | null;
}

/** Live stats for the mono header strip. Computed from the event stream;
 *  cheap to re-run on every reducer update. */
export function deriveStats(
  events: AgentEvent[],
  plan: DeepPlanView | null,
): DeepStats {
  return computeStats(events, plan);
}

/** The final, assembled ReportEvent — null until the engine emits it. */
export function deriveReport(events: AgentEvent[]): ReportEvent | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "report") return e;
  }
  return null;
}

/** Final report sections. The approved plan contains starting research
 * directions, not a promised document outline, so plan steps must never become
 * fake section placeholders. The report compiler owns section structure after
 * the evidence loop finishes. */
export interface AssemblingSection {
  id: string;
  title: string;
  state: "pending" | "writing" | "done";
  /** Populated only when ReportEvent supplies the section. */
  section: ReportSection | null;
}

export function deriveAssemblingSections(
  events: AgentEvent[],
  plan: DeepPlanView | null,
): AssemblingSection[] {
  void plan;
  const report = deriveReport(events);
  if (!report) return [];
  return report.sections.map((section) => ({
    id: section.id,
    title: section.title,
    state: "done",
    section,
  }));
}

// ---- live activity (trace) -------------------------------------------------

/** Translate the event stream into ActivityFeed items — the live progress
 *  trace's body. Reuses ActivityFeed verbatim (same prop shape). Engine
 *  internals (phase markers) are filtered to the user-visible operations. */
export function deriveLiveTrace(
  events: AgentEvent[],
  status: ConversationStatus,
): ActivityItem[] {
  return computeLiveTrace(events, status);
}

// ---- source tiers ----------------------------------------------------------

export interface SourceTiers {
  /** Earned a citation — passages referenced by some section. */
  cited: Array<Record<string, unknown>>;
  /** Read but not cited — extracted, no citation. */
  reviewed: Array<Record<string, unknown>>;
  /** Found but not read — discovered hits without successful extraction. */
  discovered: Array<Record<string, unknown>>;
}

function citedSources(report: ReportEvent): Map<string, Record<string, unknown>> {
  const byUrl = new Map<string, Record<string, unknown>>();
  for (const passage of report.passages) {
    const url = String(passage.source_url ?? "");
    if (!url) continue;
    const key = sourceUrlKey(url);
    if (!byUrl.has(key)) byUrl.set(key, passage);
  }
  return byUrl;
}

function durableReviewedSources(
  report: ReportEvent,
  citedUrls: Set<string>,
  seen: Set<string>,
): Array<Record<string, unknown>> {
  const reviewed: Array<Record<string, unknown>> = [];
  for (const passage of report.reviewed_passages ?? []) {
    const url = String(passage.source_url ?? "");
    if (!url) continue;
    const key = sourceUrlKey(url);
    if (citedUrls.has(key) || seen.has(key)) continue;
    seen.add(key);
    reviewed.push({
      ...passage,
      url,
      title: String(passage.source_title ?? ""),
      status: "ok",
    });
  }
  return reviewed;
}

function splitDiscoveryHits(
  report: ReportEvent,
  citedUrls: Set<string>,
  seen: Set<string>,
): Pick<SourceTiers, "reviewed" | "discovered"> {
  const reviewed: Array<Record<string, unknown>> = [];
  const discovered: Array<Record<string, unknown>> = [];
  for (const hit of report.all_hits) {
    const url = String(hit.url ?? "");
    if (!url) continue;
    const key = sourceUrlKey(url);
    if (citedUrls.has(key) || seen.has(key)) continue;
    seen.add(key);
    const status = String(hit.status ?? "unread");
    if (status === "ok") reviewed.push(hit);
    else discovered.push(hit);
  }
  return { reviewed, discovered };
}

/** Split the engine's corpus into three Perplexity-style tiers. Cited rows
 *  come from `report.passages`; reviewed rows come first from the durable
 *  `reviewed_passages` corpus and then successful discovery hits; discovered
 *  rows are the remaining hits that were found but not read. */
export function deriveSourceTiers(report: ReportEvent | null): SourceTiers {
  if (!report) return { cited: [], reviewed: [], discovered: [] };
  // Dedup the cited tier BY URL — a single page can produce many passages, but
  // the user-facing list is one row per source. Keep the first passage per URL
  // so the citation chip's deep-link stays stable across replays.
  // INVARIANT (2026-07-09 off-by-one fix): the row order here — first-seen
  // source_url over report.passages — IS the numbering base `citationNumbers`
  // (lib/sources.ts) assigns to the inline chips. Row [i+1] must equal the chip
  // number of every passage from that source; change one, change both (the
  // alignment test in sources.test.ts pins this).
  const citedByUrl = citedSources(report);
  const cited = Array.from(citedByUrl.values());
  const citedUrls = new Set(citedByUrl.keys());
  const seenHits = new Set<string>();
  const reviewed = durableReviewedSources(report, citedUrls, seenHits);
  const hits = splitDiscoveryHits(report, citedUrls, seenHits);
  return {
    cited,
    reviewed: [...reviewed, ...hits.reviewed],
    discovered: hits.discovered,
  };
}
