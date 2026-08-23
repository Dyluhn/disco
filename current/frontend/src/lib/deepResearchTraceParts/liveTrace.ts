/**
 * Live activity (trace) view, split out of `deepResearchTrace.ts`
 * (PKG-12-C/TS-0053). The verb vocabulary (`VERB`/`plainLabel`/
 * `truncateWithEllipsis`/`plainObservation`) and the per-action row builder
 * move together — they're the same "engine tool_call → plain-language row"
 * concern the original file grouped under "live activity (trace)".
 */
import type { ActivityItem } from "@/lib/buildTrace";
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
    const p = String(a.phase ?? "");
    if (p === "gather") return `Gathering sources`;
    if (p === "synthesize") return `Writing the report`;
    if (p === "coherence") return `Drafting the executive summary`;
    return `Phase: ${p}`;
  },
  // v2 (gateless): the model's opening read of the question — the FIRST row of
  // every run's trace. Without this arm it fell through to the unknown-kind
  // fallback and rendered as the raw internal name "brief".
  brief: () => `Framed the question`,
  search: (a) =>
    a.label != null
      ? `Iterating on section "${a.label}"`
      : `Searching: "${a.query ?? ""}"`,
  synthesize_section: (a) =>
    a.label != null
      ? `Rewriting section: ${a.label}`
      : `Writing section: ${a.section ?? ""}`,
};

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

/** Builds one ActivityFeed row for an action whose tool_name is in `VERB`
 *  (the driver already filtered out phase/section_done/unknown internals).
 *  Also handles the `synthesize_section` post-adjustment: no observation ever
 *  pairs with a section write, so this row's completion signal is the later
 *  `section_done` checkpoint (handled by the driver) — re-key it into
 *  `synthByTitle` so that checkpoint can find and finish THIS row. */
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
  } else if (tc.tool_name === "brief") {
    detail = briefDetail(tc.arguments);
  }
  let st: ActivityItem["status"];
  // The brief is a completed statement the moment it lands — no observation
  // pairs with it, so without this arm it would spin forever during the run.
  if (obs || tc.tool_name === "brief") st = "done";
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
    items.push(
      buildActivityItem(e as ActionEvent, tc, observed, status, synthByTitle),
    );
  }
  return items;
}
