/**
 * Live activity (trace) view, split out of `deepResearchTrace.ts`
 * (PKG-12-C/TS-0053). The verb vocabulary (`VERB`/`plainLabel`/
 * `truncateWithEllipsis`/`plainObservation`) and the per-action row builder
 * move together — they're the same "engine tool_call → plain-language row"
 * concern the original file grouped under "live activity (trace)".
 */
import type { ActivityItem } from "@/lib/buildTrace";
import { countOf } from "@/lib/deepResearchHeartbeat";
import { holdResumedLabel } from "@/lib/deepResearchHold";
import { splitThink } from "@/lib/think";
import type {
  ActionEvent,
  AgentEvent,
  ConversationStatus,
  ObservationEvent,
  ToolCall,
} from "@/types/agent";

/** Same verb-map pattern as buildTrace, specialized for the engine's tools. */
const VERB: Record<string, (a: Record<string, unknown>) => string> = {
  phase: (a) => {
    const p = String(a.phase ?? "").toLowerCase();
    if (p === "gather" || p === "search" || p === "reading") return `Searching sources`;
    if (p === "synthesize" || p === "synthesizing" || p === "write" || p === "writing") return `Writing the report`;
    if (p === "coherence" || p === "review" || p === "reviewing" || p === "verification") return `Reviewing the report`;
    return p ? `Research phase: ${p}` : `Researching`;
  },
  // v2 (gateless): the model's opening read of the question — the FIRST row of
  // every run's trace. Without this arm it fell through to the unknown-kind
  // fallback and rendered as the raw internal name "brief".
  brief: () => `Framed the question`,
  // `origin` says WHO issued the query. "system" is the loop re-running a query
  // of its own accord — the queue only ever holds queries that never reached an
  // engine or whose pages could not be read — and rendering it as "Searching"
  // read as the model asking the same thing twice.
  search: (a) =>
    `${a.origin === "system" ? "Re-running" : "Searching"}: "${a.query ?? a.label ?? ""}"${narrowsClause(a)}`,
  // The twin of a `search` row: a query the model proposed that never reached
  // an engine. Without it a turn that lost all three proposals rendered as
  // "searching" and then silence — the run looked stuck when it was working.
  query_refused: (a) => refusedLabel(a),
  synthesize_section: (a) =>
    a.label != null
      ? `Rewriting section: ${a.label}`
      : `Writing section: ${a.section ?? ""}`,
  // L24: a hold and its resume are real operations the run spent time in, so
  // they belong in the permanent trace — otherwise a finished report shows a
  // four-minute gap with no explanation of where it went.
  hold: () => `Waiting for search engines to cool down`,
  hold_resumed: (a) =>
    holdResumedLabel(Number(a.waited_s ?? 0), engineNames(a.engines_live)),
  // A permanent row for the same reason a hold gets one: the gap between Stop
  // and the checkpoint is real time the run spent finishing its step, and a
  // trace that skipped it would leave that stretch unexplained on replay.
  stop_requested: () => `You pressed Stop`,
};

/** Why one proposed query never ran, in the reader's words. A wall has to say
 *  what it blocked AND where the work went instead, so every class that names
 *  the query it matched, or the angle it closed, says so. The raw reason token
 *  is never rendered; an unknown one degrades to the plain fact. */
function refusedBecause(reason: string, args: Record<string, unknown>): string {
  const matched = typeof args.duplicates === "string" ? args.duplicates.trim() : "";
  const turn = Number(args.duplicates_turn);
  if (reason === "repeat" || reason === "near_duplicate") {
    if (!matched) return `this run already ran the same query`;
    return Number.isFinite(turn)
      ? `the same as turn ${turn}'s "${matched}"`
      : `the same as "${matched}"`;
  }
  if (reason === "queued") {
    return matched
      ? `the host is already re-running "${matched}"`
      : `the host is already re-running it`;
  }
  if (reason === "retry_budget_spent") {
    return `the host stopped re-running it, so its angle is untested`;
  }
  if (reason === "exhausted") {
    const angle = typeof args.angle === "string" ? args.angle.trim() : "";
    const why = typeof args.why === "string" ? args.why.trim() : "";
    const closed = angle ? `angle "${angle}" is exhausted` : `this angle is exhausted`;
    return why ? `${closed}: ${why}` : closed;
  }
  return `the host refused it`;
}

/** One `query_refused` row. The query is verbatim, exactly as the `search` row
 *  next to it renders the ones that did reach the world — the reader's whole
 *  job here is to compare the two. */
function refusedLabel(args: Record<string, unknown>): string {
  const query = String(args.query ?? "").trim();
  const reason = String(args.reason ?? "");
  if (reason === "empty" || query === "") return `Not searched: the proposal was blank`;
  return `Not searched: "${query}" — ${refusedBecause(reason, args)}`;
}

/** What the freshness wall let through, when it matched this query and issued
 *  it anyway. A narrowing repeats a tested query with a scope added, so on
 *  screen it was the same line twice with nothing to say the second was
 *  admitted on purpose. Both keys are absent on every other search, so nothing
 *  is rendered for one. */
function narrowsClause(args: Record<string, unknown>): string {
  const from = typeof args.narrowed_from === "string" ? args.narrowed_from.trim() : "";
  if (!from) return "";
  const added = Array.isArray(args.narrowing)
    ? args.narrowing.filter(
        (term): term is string => typeof term === "string" && term.trim() !== "",
      )
    : [];
  return added.length > 0
    ? ` — narrows "${from}" (adds ${added.join(", ")})`
    : ` — narrows "${from}"`;
}

/** How many pages the extractor tried on this search, when the observation's
 *  retrieval trace carried the rollup. Absent on an event that predates it. */
function extractionAttempted(structured: Record<string, unknown>): number | null {
  const trace = structured.retrieval_trace;
  if (typeof trace !== "object" || trace === null) return null;
  const extraction = (trace as Record<string, unknown>).extraction;
  if (typeof extraction !== "object" || extraction === null) return null;
  const attempted = Number((extraction as Record<string, unknown>).attempted);
  return Number.isFinite(attempted) && attempted > 0 ? attempted : null;
}

/** Why an admission came back empty, in the reader's words. The backend already
 *  classifies it (`yield_reason`) and writes a `detail` beside it — but that
 *  detail is addressed to the MODEL ("Pivot the query or trace the named source
 *  upstream"), so it is not what a reader is shown. The raw class token never
 *  reaches the screen, and a class this map does not name adds nothing rather
 *  than a guess. */
function emptyBecause(structured: Record<string, unknown>): string {
  const reason = structured.yield_reason;
  if (reason === "no_hits") return `no results`;
  if (reason === "extraction_failure") {
    const attempted = extractionAttempted(structured);
    return attempted === null
      ? `pages found, none could be read`
      : `${countOf(attempted, "page")} found, none could be read`;
  }
  if (reason === "duplicates_or_filtered") return `only sources already in the pool`;
  if (reason === "budget") return `the source budget is full`;
  if (reason === "provider_degraded") {
    return `the search engines were degraded, so this query was never tested`;
  }
  return "";
}

/** The engine names an event listed, ignoring anything that isn't a string. */
function engineNames(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((name): name is string => typeof name === "string" && name.trim() !== "");
}

/** What a hold row says under its label: which engines, and what is waiting. */
function holdDetail(args: Record<string, unknown>): string | undefined {
  const engines = Array.isArray(args.engines)
    ? args.engines.flatMap((entry) =>
        typeof entry === "object" && entry !== null && typeof (entry as { name?: unknown }).name === "string"
          ? [(entry as { name: string }).name]
          : [],
      )
    : [];
  // The QUERIES themselves, not a count of them: this is the work the hold is
  // waiting to do, and a budget denominated in work has to show the work. The
  // count leads so the line still reads at a glance.
  const queued = Array.isArray(args.queued_queries)
    ? args.queued_queries.filter(
        (query): query is string => typeof query === "string" && query.trim() !== "",
      )
    : [];
  const parts: string[] = [];
  if (engines.length > 0) parts.push(engines.join(", "));
  if (queued.length > 0) {
    parts.push(
      `${countOf(queued.length, "query", "queries")} queued: ${queued
        .map((query) => `“${query}”`)
        .join(", ")}`,
    );
  }
  return parts.length > 0 ? parts.join(" · ") : undefined;
}

/** The brief's own text is its trace detail — the reader sees HOW the model
 *  read the question, not just that it did. Truncated to one trace line; the
 *  full text renders as an assistant chat message in the report view. */
function briefDetail(args: Record<string, unknown>): string | undefined {
  const text = String(args.text ?? "").trim();
  return text ? truncateWithEllipsis(splitThink(text).answer || text, 160) : undefined;
}

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
    // "Added 0 sources ×3" never said why, so a run that hit an extraction
    // outage read as a run that found nothing to say. The reason is on the
    // event; this is where it becomes words.
    const why = emptyBecause(structured);
    const because = why ? ` — ${why}` : "";
    // Same fact, same words as the heartbeat's `observationText` — this row
    // used to read "Added 1 sources" directly under "1 new source".
    if (typeof added === "number" && typeof total === "number") {
      return `Added ${countOf(added, "source")} (${total} admitted so far)${because}`;
    }
    if (typeof added === "number") {
      return `Added ${countOf(added, "source")}${because}`;
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

/** Builds one ActivityFeed row for an action whose tool_name is in `VERB`
 *  (the driver already filtered out phase/section_done/unknown internals).
 *  Also handles the `synthesize_section` post-adjustment: no observation ever
 *  pairs with a section write, so this row's completion signal is the later
 *  `section_done` checkpoint (handled by the driver) — re-key it into
 *  `synthByTitle` so that checkpoint can find and finish THIS row. */
/** Rows whose detail comes from the ACTION's own arguments (no observation
 *  ever pairs with them). */
const DETAIL: Record<string, (a: Record<string, unknown>) => string | undefined> = {
  brief: briefDetail,
  hold: holdDetail,
};

/** Actions that are complete the moment they land — no observation follows, so
 *  without this they would spin for the rest of the run. Each one records work
 *  that DID happen (the model framed the question, the wait ended, Stop was
 *  pressed), so each one is a success. `query_refused` is not here: it settles
 *  as a VERDICT, below. */
const SETTLED = new Set(["brief", "hold_resumed", "stop_requested"]);

/** The one settled row that is not a success. A refused query never reached an
 *  engine, so it is neither the run succeeding nor failing — and the feed's
 *  green check said "done — good" over "Not searched: …". */
const VERDICT = "query_refused";

function buildActivityItem(
  e: ActionEvent,
  tc: ToolCall,
  observed: Map<string, ObservationEvent>,
  status: ConversationStatus,
  synthByTitle: Map<string, ActivityItem>,
): ActivityItem {
  const obs = observed.get(e.id);
  const label = plainLabel(tc.tool_name, tc.arguments);
  let detail: string | undefined;
  if (obs && obs.tool_result.structured) {
    detail = plainObservation(
      obs.tool_result.tool_name,
      obs.tool_result.structured,
    );
  } else {
    detail = DETAIL[tc.tool_name]?.(tc.arguments);
  }
  let st: ActivityItem["status"];
  if (tc.tool_name === VERDICT) st = "settled";
  else if (obs || SETTLED.has(tc.tool_name)) st = "done";
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
  return item;
}

/**
 * One HOLD EPISODE is one trace row. The loop re-emits `hold` as it re-syncs
 * its resume estimate, and a row per re-sync would turn a single four-minute
 * wait into a wall of identical lines. `redundant` are the re-syncs to drop;
 * `settled` are the episode-opening events whose wait later ended, so their row
 * stops running even though no observation pairs with it.
 */
function holdEpisodes(events: AgentEvent[]): {
  redundant: Set<string>;
  settled: Set<string>;
} {
  const redundant = new Set<string>();
  const settled = new Set<string>();
  let open: string | null = null;
  for (const e of events) {
    if (e.kind !== "action" || !e.tool_call) continue;
    const name = e.tool_call.tool_name;
    if (name === "hold") {
      if (open === null) open = e.id;
      else redundant.add(e.id);
    } else if (name === "hold_resumed") {
      if (open !== null) settled.add(open);
      open = null;
    }
  }
  return { redundant, settled };
}

/**
 * A run re-frames when the model revises its brief mid-run, and the engine
 * emits the same `brief` event for that as for the opening one. On screen a
 * re-frame therefore rendered as a second "Framed the question" with nothing
 * between the two lines to say why the run had gone back to the start (UI-19).
 *
 * The second and later brief rows say they are re-frames, and say what the
 * searches since the previous framing returned — which is the reason: a round
 * that admitted nothing is exactly what sends the model back to the question.
 */
function reframeLabels(events: AgentEvent[]): Map<string, string> {
  const labels = new Map<string, string>();
  let framed = false;
  let searches = 0;
  let admitted = 0;
  for (const e of events) {
    if (e.kind === "observation") {
      const result = (e as ObservationEvent).tool_result;
      const added = result.structured?.added;
      if (result.tool_name === "observation" && typeof added === "number") admitted += added;
      continue;
    }
    if (e.kind !== "action" || !e.tool_call) continue;
    if (e.tool_call.tool_name === "search") {
      searches += 1;
      continue;
    }
    if (e.tool_call.tool_name !== "brief") continue;
    if (framed) labels.set(e.id, `Re-framed the question — ${reframeReason(searches, admitted)}`);
    framed = true;
    searches = 0;
    admitted = 0;
  }
  return labels;
}

/** Why the model went back to the question, from what the searches since its
 *  last framing actually returned. */
function reframeReason(searches: number, admitted: number): string {
  if (searches === 0) return `the model revised how it reads the question`;
  if (admitted === 0) return `${countOf(searches, "search", "searches")} admitted no sources`;
  return `${countOf(searches, "search", "searches")} admitted ${countOf(admitted, "source")}`;
}

/** Translate the event stream into ActivityFeed items — the live progress
 *  trace's body. Reuses ActivityFeed verbatim (same prop shape). Engine
 *  internals (phase markers) are filtered to the user-visible operations. */
export function computeLiveTrace(
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
  const holds = holdEpisodes(events);
  const reframes = reframeLabels(events);
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
    if (holds.redundant.has(e.id)) continue; // a re-sync of a wait already shown
    const item = buildActivityItem(e as ActionEvent, tc, observed, status, synthByTitle);
    if (holds.settled.has(e.id)) item.status = "done";
    const reframe = reframes.get(e.id);
    if (reframe) item.label = reframe;
    items.push(item);
  }
  return items;
}
