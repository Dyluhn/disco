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
import { computeLiveTrace } from "./deepResearchTraceParts/liveTrace";
import { computeStats } from "./deepResearchTraceParts/stats";
import type {
  AgentEvent,
  ConversationStatus,
  ReportEvent,
  ResearchCheckpointEvent,
  ReportSection,
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
}

/** Live stats for the mono header strip. Computed from the event stream;
 *  cheap to re-run on every reducer update. */
export function deriveStats(events: AgentEvent[]): DeepStats {
  return computeStats(events);
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

/** Latest resumable state. Checkpoints never feed report/source rendering. */
export function deriveResearchCheckpoint(
  events: AgentEvent[],
): ResearchCheckpointEvent | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "research_checkpoint") return e;
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
export function deriveSourceTiers(report: ReportEvent | null): SourceTiers {
  if (!report) return { cited: [], reviewed: [], discovered: [] };
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
  };
}
