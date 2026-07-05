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
 */

import type { ActivityItem } from "@/lib/buildTrace";
import { splitThink } from "@/lib/think";
import type {
  ActionEvent,
  AgentEvent,
  ConversationStatus,
  ObservationEvent,
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
  const map = new Map<number, StepState>();
  if (!plan) return map;
  const titleIndex = new Map<string, number>();
  plan.steps.forEach((s, i) => titleIndex.set(s.title, i + 1)); // 1-based

  for (const e of events) {
    if (e.kind !== "action" || !e.tool_call) continue;
    const name = e.tool_call.tool_name;
    const args = e.tool_call.arguments;
    if (name === "search") {
      const subq = String(args.subquestion ?? "");
      const idx = titleIndex.get(subq);
      if (idx !== undefined && map.get(idx) !== "done") {
        map.set(idx, "active");
      }
    } else if (name === "synthesize_section") {
      // Writing STARTED — the step is being worked, not finished. Marking it
      // done here (the old behavior) checked steps off ~30-60s early on local
      // models and left the strip lying about progress.
      const section = String(args.section ?? "");
      const idx = titleIndex.get(section);
      if (idx !== undefined && map.get(idx) !== "done") map.set(idx, "active");
    } else if (name === "section_done") {
      const title = String(args.title ?? "");
      const idx = titleIndex.get(title);
      if (idx !== undefined) map.set(idx, "done");
    }
  }
  return map;
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
  let activeSubq: { title: string; index: number } | null = null;
  let activeRound: { current: number; max: number } | null = null;
  let sourcesDiscovered = 0;
  let phase: string | null = null;
  let activeSection: { title: string; index: number } | null = null;
  let lastThought: string | null = null;
  let firstSeq = Number.POSITIVE_INFINITY;
  let lastSeq = 0;
  const titleIndex = new Map<string, number>();
  plan?.steps.forEach((s, i) => titleIndex.set(s.title, i + 1));

  for (const e of events) {
    if (e.seq != null) {
      firstSeq = Math.min(firstSeq, e.seq);
      lastSeq = Math.max(lastSeq, e.seq);
    }
    if (e.kind === "action" && e.tool_call) {
      const name = e.tool_call.tool_name;
      const args = e.tool_call.arguments;
      if (name === "search") {
        const subq = String(args.subquestion ?? "");
        const idx = titleIndex.get(subq);
        if (idx !== undefined) {
          activeSubq = { title: subq, index: idx };
          const cur = Number(args.round);
          const max = Number(args.rounds_max);
          if (Number.isFinite(cur) && Number.isFinite(max)) {
            activeRound = { current: cur, max };
          }
        }
      } else if (name === "synthesize_section") {
        // Writing this section now — the heartbeat for the quiet stretch.
        const section = String(args.section ?? "");
        const idx = titleIndex.get(section);
        activeSection = { title: section, index: idx ?? 0 };
        if (activeSubq && activeSubq.title === section) activeSubq = null;
        activeRound = null; // rounds are a gather concept
      } else if (name === "section_done") {
        const title = String(args.title ?? "");
        if (activeSection && activeSection.title === title) activeSection = null;
        lastThought = `Finished “${title}”`;
      } else if (name === "phase") {
        phase = String(args.phase ?? "") || null;
      }
    } else if (e.kind === "observation" && (e as ObservationEvent).tool_result) {
      const r = (e as ObservationEvent).tool_result;
      if (r.tool_name === "observation" && r.structured) {
        const added = Number(r.structured.added);
        if (Number.isFinite(added)) sourcesDiscovered += added;
      } else if (r.tool_name === "gap_reason" && r.structured) {
        // The engine's between-rounds reasoning: what it found, whether it's
        // sufficient, what's still missing — the Google-style "thought" line.
        const rationale = String(r.structured.rationale ?? "").trim();
        if (rationale) lastThought = rationale;
      }
    }
  }

  const subquestionsTotal = plan?.steps.length ?? 0;
  let subquestionsDone = 0;
  if (plan) {
    const progress = derivePlanProgress(events, plan);
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
    activeSubquestion: activeSubq,
    activeRound,
    sourcesDiscovered,
    elapsedSeconds,
    phase,
    activeSection,
    lastThought,
  };
}

/** The final, assembled ReportEvent — null until the engine emits it. */
export function deriveReport(events: AgentEvent[]): ReportEvent | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "report") return e;
  }
  return null;
}

/** Placeholder section-cards rendered DURING the run, so the user sees the
 *  report's structure assembling. Each plan sub-question becomes a placeholder;
 *  when `synthesize_section` fires it transitions to "writing"; when the
 *  final ReportEvent arrives the placeholder is superseded by the real
 *  ReportSection (the surface handles the swap). */
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
  if (!plan) return [];
  const report = deriveReport(events);
  const sectionByTitle = new Map<string, ReportSection>();
  if (report) {
    for (const s of report.sections) sectionByTitle.set(s.title, s);
  }
  const writingTitles = new Set<string>();
  for (const e of events) {
    if (e.kind !== "action" || !e.tool_call) continue;
    if (e.tool_call.tool_name === "synthesize_section") {
      writingTitles.add(String(e.tool_call.arguments.section ?? ""));
    }
  }
  return plan.steps.map((step, i) => {
    const real = sectionByTitle.get(step.title);
    let state: "pending" | "writing" | "done" = "pending";
    if (real) state = "done";
    else if (writingTitles.has(step.title)) state = "writing";
    return {
      id: real?.id ?? `s${i}`,
      title: step.title,
      state,
      section: real ?? null,
    };
  });
}

// ---- live activity (trace) -------------------------------------------------

/** Same verb-map pattern as buildTrace, specialized for the engine's tools. */
const VERB: Record<string, (a: Record<string, unknown>) => string> = {
  phase: (a) => {
    const p = String(a.phase ?? "");
    if (p === "gather") return `Gathering sources`;
    if (p === "synthesize") return `Writing the report`;
    if (p === "coherence") return `Drafting the executive summary`;
    return `Phase: ${p}`;
  },
  search: (a) =>
    a.label != null
      ? `Iterating on section "${a.label}"`
      : `Searching: "${a.query ?? ""}"`,
  synthesize_section: (a) =>
    a.label != null
      ? `Rewriting section: ${a.label}`
      : `Writing section: ${a.section ?? ""}`,
};

function plainLabel(toolName: string, args: Record<string, unknown>): string {
  return (VERB[toolName] ?? (() => `${toolName}`))(args);
}

function truncateWithEllipsis(text: string, maxChars: number): string {
  return text.length > maxChars ? `${text.slice(0, maxChars)}…` : text;
}

function plainObservation(toolName: string, structured: Record<string, unknown>): string {
  if (toolName === "observation") {
    const added = structured.added;
    const total = structured.total_for_subq;
    const round = structured.round;
    if (typeof added === "number" && typeof total === "number") {
      return `Round ${round}: +${added} sources (${total} total for this sub-question)`;
    }
  }
  if (toolName === "gap_reason") {
    const sufficient = Boolean(structured.sufficient);
    const rationale = String(structured.rationale ?? "");
    return sufficient
      ? `Coverage sufficient`
      : `Gap noted: ${truncateWithEllipsis(splitThink(rationale).answer || rationale, 100)}`;
  }
  return "";
}

/** Translate the event stream into ActivityFeed items — the live progress
 *  trace's body. Reuses ActivityFeed verbatim (same prop shape). Engine
 *  internals (phase markers) are filtered to the user-visible operations. */
export function deriveLiveTrace(
  events: AgentEvent[],
  status: ConversationStatus,
): ActivityItem[] {
  const items: ActivityItem[] = [];
  const observed = new Map<string, ObservationEvent>();
  for (const e of events) {
    if (e.kind === "observation") {
      observed.set(e.action_id, e as ObservationEvent);
    }
  }
  // "Writing section: X" items keyed by section title, so the engine's
  // section_done checkpoint marks ITS row done instead of rendering as a raw
  // internal name (the "section_done" leak).
  const synthByTitle = new Map<string, ActivityItem>();
  for (const e of events) {
    if (e.kind !== "action" || !e.tool_call) continue;
    const tc = e.tool_call;
    if (tc.tool_name === "phase") continue; // engine internals — skip
    if (tc.tool_name === "section_done") {
      const item = synthByTitle.get(String(tc.arguments.title ?? ""));
      if (item) item.status = "done";
      continue; // a checkpoint, not a user-visible operation of its own
    }
    if (!(tc.tool_name in VERB)) continue; // NEVER render internals raw
    const action = e as ActionEvent;
    const obs = observed.get(action.id);
    const label = plainLabel(tc.tool_name, tc.arguments);
    let detail: string | undefined;
    if (obs && obs.tool_result.structured) {
      detail = plainObservation(
        obs.tool_result.tool_name,
        obs.tool_result.structured,
      );
    }
    let st: ActivityItem["status"];
    if (obs) st = "done";
    else if (status === "RUNNING") st = "running";
    else st = "done";
    const item: ActivityItem = {
      id: e.id,
      kind: "action",
      label,
      detail,
      status: st,
      attention: false,
    };
    if (tc.tool_name === "synthesize_section") {
      // No observation pairs with a section write — section_done (above) is its
      // completion signal. Until then it runs; on terminal status it settles.
      item.status = status === "RUNNING" ? "running" : "done";
      synthByTitle.set(String(tc.arguments.section ?? ""), item);
    }
    items.push(item);
  }
  return items;
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

/** Split the engine's corpus into three Perplexity-style tiers. Cited rows
 *  come from `report.passages`; reviewed = all_hits intersected with passage
 *  URLs (read AND turned into a passage but not cited); discovered = all_hits
 *  minus reviewed/cited (found but not extracted into a passage). */
export function deriveSourceTiers(report: ReportEvent | null): SourceTiers {
  if (!report) return { cited: [], reviewed: [], discovered: [] };
  // Dedup the cited tier BY URL — a single page can produce many passages, but
  // the user-facing list is one row per source. Keep the first passage per URL
  // so the citation chip's deep-link stays stable across replays.
  const citedByUrl = new Map<string, Record<string, unknown>>();
  for (const p of report.passages) {
    const url = String((p as Record<string, unknown>).source_url ?? "");
    if (!url) continue;
    if (!citedByUrl.has(url)) citedByUrl.set(url, p as Record<string, unknown>);
  }
  const cited = Array.from(citedByUrl.values());
  const citedUrls = new Set(citedByUrl.keys());
  const reviewed: Array<Record<string, unknown>> = [];
  const discovered: Array<Record<string, unknown>> = [];
  for (const hit of report.all_hits) {
    const url = String((hit as Record<string, unknown>).url ?? "");
    if (!url) continue;
    if (citedUrls.has(url)) continue; // cited already covered
    // a hit with status="ok" is at least "reviewed" (we fetched + extracted it
    // even if no passage was cited). status != "ok" → discovered (failed).
    const status = String((hit as Record<string, unknown>).status ?? "ok");
    if (status === "ok") reviewed.push(hit);
    else discovered.push(hit);
  }
  return { cited, reviewed, discovered };
}
