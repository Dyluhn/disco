/**
 * Live activity derivation (L24) — the latest REPORTED fact of each kind, with
 * the instant the engine reported it.
 *
 * This module exists because the run UI used to infer liveness from
 * `status === "RUNNING"`: a spinner that spun whether or not the backend had
 * said anything for ten minutes. Everything here is the opposite discipline —
 * one pass over the event log, keeping the newest `turn` / `model_activity` /
 * `search` / `observation` / `phase` / round / `hold` payload and its
 * timestamp. Nothing is synthesised. A payload that fails its shape check is
 * DROPPED rather than half-rendered, and `lastEventAt` (the newest timestamp of
 * ANY event) is what the honest activity indicator ages off.
 *
 * The public declarations + types live in `../deepResearchTrace.ts`; see that
 * file's header for why the parent keeps the declarations and the parts keep
 * the bodies.
 */
import type {
  ActiveHold,
  DatedActivity,
  DeepActivity,
  DeepObservationActivity,
  DeepPhaseActivity,
  DeepRoundActivity,
  DeepSearchActivity,
} from "@/lib/deepResearchTrace";
import type {
  AgentEvent,
  ModelActivityPayload,
  ModelActivityStage,
  ObservationEvent,
  ResearchFollowUpGroundingPayload,
  ResearchHoldPayload,
  ResearchHoldReason,
  ResearchHoldResumedPayload,
  ResearchStopRequestedPayload,
  ResearchTurnPayload,
  ResearchTurnPhase,
} from "@/types/agent";

const TURN_PHASES: readonly string[] = [
  "planning",
  "searching",
  "extracting",
  "thinking",
];

/** The `model_activity` stages that belong to THE RUN. `follow_up` is
 *  deliberately absent: a follow-up answers a finished report, so admitting its
 *  heartbeat here would make the run-level strip and heartbeat report a live
 *  model call on a run that ended. `FollowUpStatus` reads those frames. */
const MODEL_STAGES: readonly string[] = [
  "brief",
  "research_turn",
  "source_reading",
  "draft",
  "review",
  "rework",
  "continuation",
];

/** A finite number or null — the wire is untyped, so every numeric field is
 *  validated rather than coerced into NaN. */
function num(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** A non-empty trimmed string or null. */
function text(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

/** The non-empty strings of a wire list, in order. Anything that is not a list
 *  of strings yields none rather than a coerced stand-in. */
function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    const clean = text(entry);
    return clean === null ? [] : [clean];
  });
}

/** ms epoch of an ISO-8601 instant, or null when it is missing/unparseable. */
export function isoToMs(value: unknown): number | null {
  const raw = text(value);
  if (raw === null) return null;
  const ms = Date.parse(raw);
  return Number.isFinite(ms) ? ms : null;
}

function parseTurn(args: Record<string, unknown>): ResearchTurnPayload | null {
  const n = num(args.n);
  const of = num(args.of);
  const phase = typeof args.phase === "string" ? args.phase : "";
  if (n === null || of === null || !TURN_PHASES.includes(phase)) return null;
  return {
    n,
    of,
    phase: phase as ResearchTurnPhase,
    subquestion: text(args.subquestion),
  };
}

function parseModelActivity(
  args: Record<string, unknown>,
): ModelActivityPayload | null {
  const stage = typeof args.stage === "string" ? args.stage : "";
  const seconds = num(args.seconds);
  if (!MODEL_STAGES.includes(stage) || seconds === null) return null;
  const payload: ModelActivityPayload = {
    stage: stage as ModelActivityStage,
    tokens_streamed: num(args.tokens_streamed) ?? 0,
    seconds,
    call_ordinal: num(args.call_ordinal) ?? 0,
  };
  // Optional on the wire: the backend omits the key when the provider exposed
  // no reasoning channel, so an absent key stays absent here rather than
  // becoming a 0 that would read as "no thinking happened".
  const reasoning = num(args.reasoning_tokens);
  if (reasoning !== null) payload.reasoning_tokens = reasoning;
  // Same rule: present only when FALSE, which is the backend saying this is the
  // one heartbeat a buffered transport will send.
  if (args.streams === false) payload.streams = false;
  if (args.state === "waiting" || args.state === "generating" || args.state === "backoff") {
    payload.state = args.state;
  }
  if (typeof args.source_id === "string") payload.source_id = args.source_id;
  for (const key of ["chunk", "chunks", "attempt", "retry_after_s", "http_status"] as const) {
    const value = num(args[key]);
    if (value !== null && value >= 0) payload[key] = value;
  }
  return payload;
}

function parsePhase(phase: string, args: Record<string, unknown>): DeepPhaseActivity {
  const payload: DeepPhaseActivity = { phase, wordsStreamed: num(args.words_streamed) };
  const source = num(args.source), total = num(args.of);
  if (source !== null && total !== null) {
    payload.source = source;
    payload.sourcesTotal = total;
  }
  return payload;
}

function parseHold(args: Record<string, unknown>): ResearchHoldPayload | null {
  const resumeAt = text(args.resume_at);
  if (resumeAt === null) return null;
  const rawEngines = Array.isArray(args.engines) ? args.engines : [];
  const engines = rawEngines.flatMap((entry) => {
    if (typeof entry !== "object" || entry === null) return [];
    const row = entry as Record<string, unknown>;
    const name = text(row.name);
    return name === null ? [] : [{ name, resume_at: text(row.resume_at) ?? "" }];
  });
  const turnN = num((args.turn as Record<string, unknown> | undefined)?.n);
  const turnOf = num((args.turn as Record<string, unknown> | undefined)?.of);
  // Unknown reasons collapse to the cooling case rather than leaking a raw
  // backend token into user-facing copy.
  const reason: ResearchHoldReason =
    args.reason === "search_rate_starved" ? "search_rate_starved" : "search_pool_cooling";
  return {
    reason,
    engines,
    resume_at: resumeAt,
    sources_retained: num(args.sources_retained) ?? 0,
    turn: turnN !== null && turnOf !== null ? { n: turnN, of: turnOf } : { n: 0, of: 0 },
    queued_queries: strings(args.queued_queries),
  };
}

function parseHoldResumed(
  args: Record<string, unknown>,
): ResearchHoldResumedPayload | null {
  const waited = num(args.waited_s);
  if (waited === null) return null;
  const raw = Array.isArray(args.engines_live) ? args.engines_live : [];
  return {
    waited_s: waited,
    engines_live: raw.flatMap((name) => {
      const clean = text(name);
      return clean === null ? [] : [clean];
    }),
  };
}

function parseRound(
  name: string,
  args: Record<string, unknown>,
): DeepRoundActivity | null {
  const k = num(args.k);
  const of = num(args.of);
  if (k === null || of === null) return null;
  return { kind: name as DeepRoundActivity["kind"], k, of };
}

/** The observation's own extraction rollup, when the retrieval trace carried
 *  one. This is a FINISHED count (attempted/succeeded), not live progress —
 *  the backend has no per-page progress event. */
function extractionRollup(
  structured: Record<string, unknown>,
): DeepObservationActivity["extraction"] {
  const trace = structured.retrieval_trace;
  if (typeof trace !== "object" || trace === null) return null;
  const extraction = (trace as Record<string, unknown>).extraction;
  if (typeof extraction !== "object" || extraction === null) return null;
  const row = extraction as Record<string, unknown>;
  const attempted = num(row.attempted);
  const success = num(row.success);
  if (attempted === null || success === null || attempted <= 0) return null;
  return { attempted, success };
}

/** Mutable working state for the single pass. */
interface ActivityAccumulator {
  turn: DatedActivity<ResearchTurnPayload> | null;
  modelActivity: DatedActivity<ModelActivityPayload> | null;
  search: DatedActivity<DeepSearchActivity> | null;
  observation: DatedActivity<DeepObservationActivity> | null;
  phase: DatedActivity<DeepPhaseActivity> | null;
  round: DatedActivity<DeepRoundActivity> | null;
  hold: ActiveHold | null;
  resumed: DatedActivity<ResearchHoldResumedPayload> | null;
  stopRequested: DatedActivity<ResearchStopRequestedPayload> | null;
  lastEventAt: number | null;
}

function applyHold(
  acc: ActivityAccumulator,
  eventId: string,
  at: number | null,
  payload: ResearchHoldPayload,
): void {
  // One HOLD EPISODE keeps the first event's id as its identity so the user's
  // "keep waiting" choice survives the re-synced updates that follow, while
  // the payload + countdown always come from the newest event.
  acc.hold = {
    episodeId: acc.hold?.episodeId ?? eventId,
    payload,
    at,
    resumeAtMs: isoToMs(payload.resume_at),
  };
  acc.resumed = null;
}

function applyAction(
  acc: ActivityAccumulator,
  eventId: string,
  at: number | null,
  name: string,
  args: Record<string, unknown>,
): void {
  if (name === "turn") {
    const payload = parseTurn(args);
    if (payload) acc.turn = { payload, at };
  } else if (name === "model_activity") {
    const payload = parseModelActivity(args);
    if (payload) acc.modelActivity = { payload, at };
  } else if (name === "search") {
    const query = text(args.query) ?? text(args.label);
    if (query) acc.search = { payload: { query }, at };
  } else if (name === "phase") {
    const phase = text(args.phase);
    if (phase) {
      acc.phase = { payload: parsePhase(phase, args), at };
    }
  } else if (name === "review" || name === "rework" || name === "continuation") {
    const payload = parseRound(name, args);
    if (payload) acc.round = { payload, at };
  } else {
    applyControlAction(acc, eventId, at, name, args);
  }
}

function applyControlAction(
  acc: ActivityAccumulator,
  eventId: string,
  at: number | null,
  name: string,
  args: Record<string, unknown>,
): void {
  if (name === "hold") {
    const payload = parseHold(args);
    if (payload) applyHold(acc, eventId, at, payload);
  } else if (name === "hold_resumed") {
    const payload = parseHoldResumed(args);
    if (payload) {
      acc.hold = null;
      acc.resumed = { payload, at };
    }
  } else if (name === "stop_requested") {
    const requestedAt = text(args.requested_at);
    if (requestedAt) acc.stopRequested = { payload: { requested_at: requestedAt }, at };
  }
}

function applyObservation(
  acc: ActivityAccumulator,
  at: number | null,
  event: ObservationEvent,
): void {
  const result = event.tool_result;
  if (result.tool_name !== "observation" || !result.structured) return;
  const added = num(result.structured.added);
  if (added === null) return;
  acc.observation = {
    payload: { added, extraction: extractionRollup(result.structured) },
    at,
  };
}

/** One pass over the event log → the newest reported fact of each kind. */
export function computeActivity(events: AgentEvent[]): DeepActivity {
  const acc: ActivityAccumulator = {
    turn: null,
    modelActivity: null,
    search: null,
    observation: null,
    phase: null,
    round: null,
    hold: null,
    resumed: null,
    stopRequested: null,
    lastEventAt: null,
  };
  for (const event of events) {
    const at = isoToMs(event.timestamp);
    if (at !== null) {
      acc.lastEventAt = acc.lastEventAt === null ? at : Math.max(acc.lastEventAt, at);
    }
    if (event.kind === "action" && event.tool_call) {
      applyAction(acc, event.id, at, event.tool_call.tool_name, event.tool_call.arguments);
    } else if (event.kind === "observation") {
      applyObservation(acc, at, event);
    } else if (event.kind === "status") {
      // `stop_requested` means "Stop was pressed and the run has not answered
      // yet". Any status the run writes afterwards IS the answer — the PAUSED
      // checkpoint it stops at, or the RUNNING a later resume opens with — so
      // the marker clears and a replayed run never re-raises a stale wall.
      acc.stopRequested = null;
    }
  }
  return { ...acc };
}

/**
 * The grounding ledger of the NEWEST follow-up answer, or null.
 *
 * Kept out of `computeActivity` on purpose: everything there is live-run
 * liveness that ages, and this is a durable attribute of one finished answer.
 * A caller that wants the ledger for a particular answer slices the events at
 * that message and reads the last ledger before it.
 */
export function computeFollowUpGrounding(
  events: AgentEvent[],
): ResearchFollowUpGroundingPayload | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (
      event.kind !== "action" ||
      event.tool_call?.tool_name !== "follow_up_grounding"
    ) {
      continue;
    }
    const args = event.tool_call.arguments;
    const statements = num(args.statements);
    const supported = num(args.supported);
    const weak = num(args.weak);
    const removed = num(args.removed);
    if (
      statements === null ||
      supported === null ||
      weak === null ||
      removed === null ||
      typeof args.refused !== "boolean"
    ) {
      return null; // a payload that fails its shape check is dropped, not guessed
    }
    return { statements, supported, weak, removed, refused: args.refused };
  }
  return null;
}
