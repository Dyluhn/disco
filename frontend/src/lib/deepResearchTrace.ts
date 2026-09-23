/**
 * Deep Research derivers — pure functions that turn the conversation's event
 * stream into the views the surface renders. Mirrors `buildTrace.ts` in
 * shape but specialized for the engine's tool names and the ReportEvent shape.
 *
 * v2 (gateless deep research): there is no plan and no approval gate —
 * research starts on submit and the model's brief is the first streamed
 * output. The engine emits ActionEvents with tool names:
 *   "brief"               — the model's opening read of the question ({text})
 *   "phase"               — phase transitions emitted by the engine
 *   "search"              — a retrieval query ({query, label})
 * And ObservationEvents with tool_name "observation" for admitted evidence.
 * Plus "section_done" checkpoints as the written report finalizes. The final
 * ReportEvent is the assembled report. Pre-v2 replays additionally carry
 * "synthesize_section" actions and "gap_reason" observations — their handlers
 * stay so historical conversations keep rendering.
 *
 * Module-size and complexity decomposition (PKG-12-C/TS-0051..TS-0053): the
 * highest-mccabe derivers' implementations live in `./deepResearchTraceParts/*`
 * so each callable stays under the architecture caps.
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
import { canonicalWorkKey } from "@/lib/sources";
import { computeActivity } from "./deepResearchTraceParts/activity";
import { computeLiveTrace } from "./deepResearchTraceParts/liveTrace";
import { computeStats } from "./deepResearchTraceParts/stats";
import type {
  AgentEvent,
  ConversationStatus,
  ModelActivityPayload,
  ReportEvent,
  ResearchCheckpointEvent,
  ReportSection,
  ResearchHoldPayload,
  ResearchHoldResumedPayload,
  ResearchStopRequestedPayload,
  ResearchTurnPayload,
} from "@/types/agent";

/** The model's opening brief — how it read the question and the angles it
 *  will chase. First streamed output of a gateless run: arrives as an
 *  ActionEvent (tool_name "brief", payload {text}) and is mirrored as an
 *  assistant chat message. Prefer the typed action; fall back to the first
 *  pre-report assistant message so a stream missing the action still
 *  surfaces it. Null until the brief lands. */
export function deriveBrief(events: AgentEvent[]): string | null {
  for (const e of events) {
    if (e.kind === "action" && e.tool_call?.tool_name === "brief") {
      const text = String(e.tool_call.arguments.text ?? "").trim();
      if (text) return text;
    }
  }
  const reportSeq = deriveReport(events)?.seq ?? Number.POSITIVE_INFINITY;
  for (const e of events) {
    if (e.kind !== "message" || e.message.role !== "assistant") continue;
    if ((e.seq ?? 0) >= reportSeq) break;
    const text = (e.message.content ?? "").trim();
    if (text && !text.includes("<system-reminder>")) return text;
  }
  return null;
}

export interface DeepStats {
  /** Retrieval queries fired so far (one per engine `search` action). */
  searches: number;
  /** Queries the model proposed that the host refused, so they never reached
   *  an engine (one per `query_refused` action). Counted beside `searches`
   *  because a turn can propose three and fire none — "0 searches" alone would
   *  read as a stalled run rather than a working one. */
  refusals: number;
  /** Report sections finalized (`section_done` checkpoints seen). */
  sectionsDone: number;
  /** Sources admitted so far — sum of observation `added` counts. */
  sourcesDiscovered: number;
  /** Wall-clock seconds between the first and the latest event timestamps
   *  (BaseEvent.timestamp). Null until a timestamped event exists. */
  elapsedSeconds: number | null;
  /** The engine phase from the latest `phase` action. */
  phase: string | null;
  /** Section currently being WRITTEN (pre-v2 replays' synthesize_section
   *  heartbeat; v2 writes the report in one pass). */
  activeSection: { title: string } | null;
  /** The engine's latest "thought" — a gap rationale ("found X, still
   *  missing Y") or a section completion — surfaced so a working run never
   *  reads as a hang. */
  lastThought: string | null;
  /** Which leg `lastThought` describes. A gap rationale is research-leg
   *  reasoning; a finished section is the writer's. The strip needs the
   *  difference: once a writer-leg fact is the newest thing reported, a
   *  research-leg rationale under it says the run is still searching. */
  lastThoughtLeg: "research" | "writer" | null;
  /** Sources this run carried in from a checkpoint it resumed, or null when it
   *  did not resume one. A resumed run gets a FRESH turn budget but keeps the
   *  pool, so the strip has to name the carried part — otherwise "Turn 1 of 8"
   *  over nine sources reads as a miscount. */
  carriedSources: number | null;
}

/** Live stats for the mono header strip. Computed from the event stream;
 *  cheap to re-run on every reducer update. */
export function deriveStats(events: AgentEvent[]): DeepStats {
  return computeStats(events);
}

// ---- live activity (L24) ---------------------------------------------------

/** One reported fact plus WHEN the engine reported it. `at` is null only when
 *  the event carried no parseable timestamp — then the fact still renders, but
 *  without an age, because the age would be a guess. */
export interface DatedActivity<T> {
  payload: T;
  at: number | null;
}

export interface DeepSearchActivity {
  query: string;
}

export interface DeepObservationActivity {
  /** Sources admitted by this search. */
  added: number;
  /** Pages the extractor attempted and how many it read. A FINISHED rollup
   *  carried on the observation — the backend emits no per-page progress, so
   *  this is never rendered as a live "extracting N of M". Null when absent. */
  extraction: { attempted: number; success: number } | null;
}

export interface DeepPhaseActivity {
  phase: string;
  source?: number;
  sourcesTotal?: number;
  /** Words streamed so far by the writer, when the phase event carried it. */
  wordsStreamed: number | null;
}

export interface DeepRoundActivity {
  kind: "review" | "rework" | "continuation";
  k: number;
  of: number;
}

/** The hold the loop is in RIGHT NOW (null once `hold_resumed` lands). One
 *  episode may re-emit `hold` as it re-syncs its estimate; `episodeId` stays
 *  the first event's id so a user's "keep waiting" choice is not re-asked. */
export interface ActiveHold {
  episodeId: string;
  payload: ResearchHoldPayload;
  at: number | null;
  /** ms epoch of `payload.resume_at`, or null when unparseable. */
  resumeAtMs: number | null;
}

/** Everything the engine has actually REPORTED about the run in flight, each
 *  item stamped with when it said so. Nothing here is inferred from `status`:
 *  a field is null because no event set it, and `lastEventAt` is how the UI
 *  tells "working" from "silent" without pretending. */
export interface DeepActivity {
  turn: DatedActivity<ResearchTurnPayload> | null;
  modelActivity: DatedActivity<ModelActivityPayload> | null;
  search: DatedActivity<DeepSearchActivity> | null;
  observation: DatedActivity<DeepObservationActivity> | null;
  phase: DatedActivity<DeepPhaseActivity> | null;
  round: DatedActivity<DeepRoundActivity> | null;
  hold: ActiveHold | null;
  resumed: DatedActivity<ResearchHoldResumedPayload> | null;
  /** The user pressed Stop and the run has not written a status since. Null
   *  once it has — the checkpoint it stopped at, or the RUNNING a resume
   *  opened with — so this is "stopping", never "was once stopped". */
  stopRequested: DatedActivity<ResearchStopRequestedPayload> | null;
  /** ms epoch of the newest timestamped event of ANY kind — the signal clock. */
  lastEventAt: number | null;
}

/** The latest reported fact of each kind, with its timestamp. Pure; cheap to
 *  re-run on every reducer update. */
export function deriveActivity(events: AgentEvent[]): DeepActivity {
  return computeActivity(events);
}

/** The final, assembled ReportEvent — null until the engine emits it. */
export function deriveReport(events: AgentEvent[]): ReportEvent | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (
      e.kind === "report" &&
      e.summary.trim() &&
      e.sections.length > 0 &&
      e.sections.every((section) => section.title.trim() && section.markdown.trim())
    ) {
      return e;
    }
  }
  return null;
}

/**
 * The checkpoint the run is resumable FROM right now, or null.
 *
 * A checkpoint is resumable state, not history: it describes where the run
 * stopped, and it stops describing anything the moment the run moves on. The
 * scan therefore walks back only as far as the newest status — while that
 * status is PAUSED the checkpoint is the run's current state, and any other
 * status (the RUNNING a resume opens with, the IDLE a kill lands, an ERROR)
 * means the run is somewhere else now. A report ends it the same way.
 *
 * Without that stop condition the newest checkpoint was returned forever: after
 * a resume the surface carried "Research paused before a report was written ·
 * N sources retained. Resume to continue…" over a run that was visibly working,
 * with Stop live beside it (live2/stop-captures.json, capture `93-resumed`).
 * Checkpoints never feed report/source rendering either way.
 */
export function deriveResearchCheckpoint(
  events: AgentEvent[],
): ResearchCheckpointEvent | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "research_checkpoint") return e;
    if (e.kind === "report") return null;
    if (e.kind === "status" && e.status !== "PAUSED") return null;
  }
  return null;
}

/** Final report sections. Section structure is owned by the report writer
 * after the evidence loop finishes — mid-run activity must never become fake
 * section placeholders. */
export interface AssemblingSection {
  id: string;
  title: string;
  state: "pending" | "writing" | "done";
  /** Populated only when ReportEvent supplies the section. */
  section: ReportSection | null;
}

export function deriveAssemblingSections(events: AgentEvent[]): AssemblingSection[] {
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
  /** The sources the report event does NOT carry, when it says so. Null unless
   *  discovery rows were actually dropped — a report that fit adds no line. */
  notKept: { found: number; kept: number } | null;
}

function citedSources(report: ReportEvent): Map<string, Record<string, unknown>> {
  const byUrl = new Map<string, Record<string, unknown>>();
  for (const passage of report.passages) {
    const url = String(passage.source_url ?? "");
    if (!url) continue;
    const key = canonicalWorkKey(url);
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
    const key = canonicalWorkKey(url);
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
    const key = canonicalWorkKey(url);
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
/**
 * How many discovery rows the report event started with, and how many it kept.
 *
 * A report over the 1 MiB event budget is trimmed by
 * `_report_event.fit_report_event`, which excerpts evidence and — under real
 * pressure — drops discovery rows outright. It records the total it began with
 * on the event (`meta.all_hits_total`); the panel counted only the rows it
 * received, so an exhaustive run listed 744 of 1,838 sources and said nothing
 * about the other 1,094.
 *
 * Null unless rows were actually dropped, so a report that fit adds no line.
 * `meta.reviewed_passages_total` is deliberately not read here: comparing it to
 * the rows received would need the status split of the dropped hits, which the
 * event does not carry, and inventing that split is exactly the thing this
 * change exists to stop.
 */
function discoveryNotKept(report: ReportEvent): SourceTiers["notKept"] {
  const found = Number(report.meta?.all_hits_total);
  const kept = report.all_hits.length;
  return Number.isFinite(found) && found > kept ? { found, kept } : null;
}

export function deriveSourceTiers(report: ReportEvent | null): SourceTiers {
  if (!report) return { cited: [], reviewed: [], discovered: [], notKept: null };
  // Dedup the cited tier BY WORK — a single page can produce many passages,
  // and DOI/arXiv/PMID mirrors can represent one work. Keep the first passage
  // per work
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
    notKept: discoveryNotKept(report),
  };
}
