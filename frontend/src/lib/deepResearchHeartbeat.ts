/**
 * Deep Research heartbeat + activity-signal COPY (L24). Pure functions that
 * turn `DeepActivity` (what the engine reported, and when) into the exact
 * strings the run UI shows.
 *
 * The rule these functions exist to keep: every line is the latest event of its
 * kind plus its age. Nothing here reads `status`, so nothing here can claim the
 * run is working when the backend has gone quiet — that case gets its own
 * honest words ("quiet for 35 s", "no signal for 1:12") instead of a spinner.
 *
 * Keeping the copy here (not inline in JSX) is what makes each state assertable
 * from a fixture without mounting the app.
 */
import type { DeepActivity } from "@/lib/deepResearchTrace";

/** Under this many seconds since the last event, the run is visibly live. */
export const SIGNAL_QUIET_AFTER_S = 20;
/** At or past this many seconds, the UI says there is no signal at all. */
export const SIGNAL_SILENT_AFTER_S = 60;

/** How long to keep the query in the heartbeat before eliding it. */
const QUERY_CHARS = 72;

const NUMBER = new Intl.NumberFormat("en-US");

/** A count and its noun, agreeing: "1 source", "12 sources".
 *
 *  Shared with the live trace (`deepResearchTraceParts/liveTrace.ts`) because
 *  the two surfaces render the SAME fact from the same observation and used to
 *  disagree about it — the heartbeat said "1 new source" while the trace row
 *  under it said "Added 1 sources". `plural` defaults to `${singular}s`. */
export function countOf(
  n: number,
  singular: string,
  plural = `${singular}s`,
): string {
  return `${NUMBER.format(n)} ${n === 1 ? singular : plural}`;
}

/** Seconds under a minute, m:ss under an hour, then Nh Nm, then Nd. The coarse
 *  tail matters: replaying a months-old conversation must read "89d", not a
 *  five-digit clock. */
export function formatDuration(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole} s`;
  const mins = Math.floor(whole / 60);
  if (mins < 60) return `${mins}:${String(whole % 60).padStart(2, "0")}`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ${mins % 60}m`;
  return `${Math.floor(hours / 24)}d`;
}

/** Whole seconds between a reported instant and now; never negative (a server
 *  clock slightly ahead of the browser must not read as a future event). */
export function ageSeconds(at: number | null, nowMs: number): number | null {
  if (at === null) return null;
  return Math.max(0, Math.floor((nowMs - at) / 1000));
}

export type SignalLevel = "live" | "buffered" | "backoff" | "quiet" | "silent";

export interface SignalReading {
  level: SignalLevel;
  /** Seconds since the newest event (or since the stream opened). */
  ageSeconds: number;
  /** The words shown next to the indicator. */
  label: string;
}

/**
 * How alive the run looks, measured ONLY by the age of the newest event.
 *
 * `sinceMs` is the fallback origin used before any event has arrived — the
 * moment this client opened the stream. Ageing off it says exactly what is
 * true ("we have heard nothing since we connected"), which is the honest
 * version of the old start-up spinner.
 *
 * `buffered` is the one thing the age alone cannot know: on a driver that does
 * not stream (`modelIsBuffered`), the newest event IS the call opening, and the
 * quiet after it belongs to the transport. Without it the chip warned "no
 * signal for 2:00" beside a strip line saying the driver does not report
 * progress while it works — two sentences on one screen disagreeing about the
 * same fact. Both now come from that one fact, in the same words.
 */
export function signalReading(
  lastEventAt: number | null,
  nowMs: number,
  sinceMs: number,
  buffered = false,
  retryRemaining: number | null = null,
): SignalReading {
  const age = Math.max(0, Math.floor((nowMs - (lastEventAt ?? sinceMs)) / 1000));
  if (retryRemaining !== null && retryRemaining > 0) {
    return { level: "backoff", ageSeconds: age, label: `Provider backoff · ${formatDuration(retryRemaining)}` };
  }
  if (age < SIGNAL_QUIET_AFTER_S) return { level: "live", ageSeconds: age, label: "Live" };
  if (buffered) {
    return { level: "buffered", ageSeconds: age, label: `Waiting for model reply · ${formatDuration(age)}` };
  }
  if (age < SIGNAL_SILENT_AFTER_S) {
    return { level: "quiet", ageSeconds: age, label: `quiet for ${formatDuration(age)}` };
  }
  return { level: "silent", ageSeconds: age, label: `no signal for ${formatDuration(age)}` };
}

/** Plain-language name for an engine phase. Shared by the stats row, the
 *  heartbeat and the writer line so they can never disagree. */
export function researchPhaseLabel(phase: string): string {
  const p = phase.toLowerCase();
  if (["gather", "search", "searching", "reading"].includes(p)) {
    return "Searching sources";
  }
  if (["synthesize", "synthesizing", "write", "writing", "draft", "drafting"].includes(p)) {
    return "Writing the report";
  }
  if (["coherence", "review", "reviewing", "verification"].includes(p)) {
    return "Reviewing the report";
  }
  if (p === "rework") return "Reworking the report";
  if (p === "continuation") return "Continuing the report";
  return `Research phase: ${phase}`;
}

/** What a research turn is doing, in the user's language rather than the
 *  loop's. */
const TURN_PHASE_WORD: Record<string, string> = {
  planning: "planning the next searches",
  searching: "searching",
  extracting: "reading pages",
  thinking: "thinking",
};

const ROUND_WORD: Record<string, string> = {
  review: "Review",
  rework: "Rework",
  continuation: "Continuation",
};

/** Engine `phase` values that mean the WRITER is working — the evidence loop is
 *  over. Same vocabulary `researchPhaseLabel` maps to the writer's words. */
const WRITER_PHASES: readonly string[] = [
  "reading",
  "synthesize",
  "synthesizing",
  "write",
  "writing",
  "draft",
  "drafting",
  "coherence",
  "review",
  "reviewing",
  "verification",
  "rework",
  "continuation",
];

/** `model_activity` stages that belong to the writer's calls, not a turn's. */
const WRITER_STAGES: readonly string[] = ["source_reading", "draft", "review", "rework", "continuation"];

function isWriterPhase(activity: DeepActivity): boolean {
  const phase = activity.phase;
  return phase !== null && WRITER_PHASES.includes(phase.payload.phase.toLowerCase());
}

/** Newest of a set of reported instants; an undateable fact sorts last. */
function newest(instants: Array<number | null>): number {
  return instants.reduce<number>(
    (best, at) => Math.max(best, at ?? Number.NEGATIVE_INFINITY),
    Number.NEGATIVE_INFINITY,
  );
}

/**
 * Whether the run has LEFT the research leg — the newest thing the engine
 * reported is a writer-phase fact.
 *
 * S1's `72-writing.png` is why this exists: the now-line said "Writing the
 * report" with "Turn 16 of 16 · searching" and a research subquestion directly
 * under it. Both lines were the newest event OF THEIR OWN KIND, so both were
 * individually true and together they described two states at once. A turn line
 * is only about the leg the run is still in, so once a writer fact leads, the
 * turn line and its subquestion have nothing left to describe. Ties go to the
 * writer: the writer's fact is the one that came after.
 *
 * Exported because the progress strip renders one more research-leg line the
 * reading does not own — `stats.lastThought`, which is a gap rationale when it
 * came from the research leg — and the rule for it is this same one.
 */
export function writerLegLeads(activity: DeepActivity): boolean {
  const model = activity.modelActivity;
  const writerPhase = isWriterPhase(activity);
  const writer = newest([
    activity.round?.at ?? null,
    writerPhase ? (activity.phase?.at ?? null) : null,
    model && WRITER_STAGES.includes(model.payload.stage) ? model.at : null,
  ]);
  const hasWriter =
    activity.round !== null ||
    writerPhase ||
    (model !== null && WRITER_STAGES.includes(model.payload.stage));
  if (!hasWriter) return false;
  const research = newest([
    activity.turn?.at ?? null,
    activity.search?.at ?? null,
    activity.observation?.at ?? null,
    writerPhase ? null : (activity.phase?.at ?? null),
  ]);
  return writer >= research;
}

/**
 * Whether the model call in flight is on a BUFFERED transport.
 *
 * A Responses-API driver (and the one-shot fallback for a server that refuses
 * to stream) delivers the whole answer in one response, so the backend reports
 * once at call start and then has nothing to report until it returns
 * (`model_activity.streams === false`). The quiet after that report is the
 * transport's, not the model's — without the difference the run reads as a
 * stall for the length of every call.
 */
export function modelIsBuffered(activity: DeepActivity): boolean {
  const model = activity.modelActivity;
  if (model?.payload.streams !== false) return false;
  // A later phase/search observation establishes that the buffered call returned.
  // User steering and unrelated events do not establish that fact.
  const transitions = [activity.turn, activity.phase, activity.search,
    activity.observation, activity.round, activity.hold, activity.resumed];
  return !transitions.some((event) => event?.at != null
    && event.at > (model.at ?? Number.NEGATIVE_INFINITY));
}

/** The research leg, once it is over, as what it PRODUCED rather than as a
 *  turn still in flight. `n` is the last turn the engine reported. Null when no
 *  turn was ever reported — then the line is simply dropped. */
function researchDoneLine(activity: DeepActivity): string | null {
  const turn = activity.turn;
  if (!turn) return null;
  return `Research done · ${countOf(turn.payload.n, "turn")}`;
}

function withAge(text: string, at: number | null, nowMs: number): string {
  const age = ageSeconds(at, nowMs);
  if (age === null || age < 1) return text;
  return `${text} · ${formatDuration(age)}`;
}

function ellipsis(text: string): string {
  return text.length > QUERY_CHARS ? `${text.slice(0, QUERY_CHARS)}…` : text;
}

/**
 * How long the current turn has been running.
 *
 * Prefers `model_activity.seconds` — the backend's own measurement of an open
 * model stream — but only when that heartbeat is at least as new as the turn
 * it would be describing. An older `model_activity` belongs to a previous call,
 * so the turn event's age is used instead. Never both, never a sum.
 */
function turnDuration(activity: DeepActivity, nowMs: number): string | null {
  const turn = activity.turn;
  if (!turn) return null;
  const model = activity.modelActivity;
  const modelIsCurrent =
    model !== null && (turn.at === null || model.at === null || model.at >= turn.at);
  if (model && modelIsCurrent) return formatDuration(model.payload.seconds);
  const age = ageSeconds(turn.at, nowMs);
  return age === null ? null : formatDuration(age);
}

function turnText(activity: DeepActivity, nowMs: number, withDuration: boolean): string | null {
  const turn = activity.turn;
  if (!turn) return null;
  const { n, of, phase } = turn.payload;
  const word = TURN_PHASE_WORD[phase] ?? phase;
  const head = `Turn ${NUMBER.format(n)} of ${NUMBER.format(of)} · ${word}`;
  if (!withDuration) return head;
  const duration = turnDuration(activity, nowMs);
  return duration === null ? head : `${head} ${duration}`;
}

function observationText(activity: DeepActivity): string | null {
  const observation = activity.observation;
  if (!observation) return null;
  const { added, extraction } = observation.payload;
  const sources = added > 0 ? countOf(added, "new source") : "no new sources";
  if (!extraction) return `Read the results — ${sources}`;
  return `Read ${NUMBER.format(extraction.success)} of ${NUMBER.format(extraction.attempted)} pages — ${sources}`;
}

/**
 * Whether the model is producing REASONING and nothing else right now.
 *
 * `model_activity.reasoning_tokens` is the reasoning-channel share of
 * `tokens_streamed` (absent when the provider exposes no reasoning channel), so
 * "every delta that arrived was reasoning" is an observation, not a guess. It is
 * the difference between a writer producing nothing and a writer producing
 * where the reader cannot see it yet — which otherwise reads as a stall with no
 * words on screen. Only counted when the heartbeat is at least as new as the
 * phase it would be describing.
 */
function reasoningOnly(activity: DeepActivity, since: number | null): boolean {
  const model = activity.modelActivity;
  if (!model) return false;
  if (since !== null && model.at !== null && model.at < since) return false;
  const { tokens_streamed: streamed, reasoning_tokens: reasoning } = model.payload;
  return reasoning !== undefined && reasoning > 0 && reasoning >= streamed;
}

export function modelRetryRemaining(activity: DeepActivity, nowMs: number): number | null {
  const model = activity.modelActivity;
  if (!model || model.payload.state !== "backoff") return null;
  const transitions = [activity.phase, activity.turn, activity.round];
  if (transitions.some((event) => event?.at != null && event.at > (model.at ?? 0))) return null;
  return Math.max(0, (model.payload.retry_after_s ?? 0) - (ageSeconds(model.at, nowMs) ?? 0));
}

function modelStatusText(activity: DeepActivity, nowMs: number): string | null {
  const status = modelProgressText(activity, nowMs);
  if (!activity.modelActivity?.payload.reasoning_control_fallback) return status;
  return `${status ?? "Model request"} · retrying without optional reasoning settings`;
}

function modelProgressText(activity: DeepActivity, nowMs: number): string | null {
  const model = activity.modelActivity;
  if (!model) return null;
  const p = model.payload;
  if (p.state === "backoff") {
    const elapsed = ageSeconds(model.at, nowMs) ?? 0;
    const remaining = Math.max(0, (p.retry_after_s ?? 0) - elapsed);
    return remaining > 0
      ? `Provider retry ${p.attempt ?? 1} · waiting ${formatDuration(remaining)}`
      : "Waiting for provider retry to start";
  }
  const phase = activity.phase?.payload;
  const source = phase?.source && phase.sourcesTotal
    ? `source ${phase.source} of ${phase.sourcesTotal}` : "source";
  const chunk = p.chunk && p.chunks ? ` · part ${p.chunk} of ${p.chunks}` : "";
  if (p.state === "waiting") return `Waiting for first model output${chunk}`;
  if (p.stage === "source_reading") {
    return `Reading ${source}${chunk} · ${reasoningOnly(activity, model.at) ? "thinking" : "receiving notes"}`;
  }
  return modelIsBuffered(activity)
    ? "Waiting for model reply (this driver does not report progress while it works)" : null;
}

function phaseText(activity: DeepActivity): string | null {
  const phase = activity.phase;
  if (!phase) return null;
  const label = researchPhaseLabel(phase.payload.phase);
  const words = phase.payload.wordsStreamed;
  if (words !== null && words > 0) return `${label}: ${NUMBER.format(words)} words`;
  return reasoningOnly(activity, phase.at) ? `${label}: thinking, no words yet` : label;
}

export interface HeartbeatReading {
  /** The one "what is happening now" line — null when no event says anything. */
  now: string | null;
  /** Turn context, when the now-line is not itself the turn. */
  turn: string | null;
  /** The subquestion the current turn is working, when the engine named one. */
  subquestion: string | null;
}

/** The newest reported fact wins the now-line; each candidate renders in its
 *  own vocabulary. `at === null` (an event with no timestamp) sorts last, so a
 *  timestamped fact is always preferred over an undateable one. */
function pickNow(
  activity: DeepActivity,
  nowMs: number,
  writerLeads: boolean,
): { key: string; at: number | null; text: string } | null {
  const candidates: Array<{ key: string; at: number | null; text: string | null }> = [
    { key: "round", at: activity.round?.at ?? null, text: activity.round
        ? `${ROUND_WORD[activity.round.payload.kind]} ${activity.round.payload.k} of ${activity.round.payload.of}`
        : null },
    { key: "search", at: activity.search?.at ?? null, text: activity.search
        ? `Searching: “${ellipsis(activity.search.payload.query)}”`
        : null },
    { key: "observation", at: activity.observation?.at ?? null, text: observationText(activity) },
    // A streamed heartbeat is only the duration source, never the now-line. A
    // BUFFERED one is the last thing that will be reported until the call
    // returns, so it has to speak for itself or the run goes silent behind it.
    { key: "model", at: activity.modelActivity?.at ?? null, text: modelStatusText(activity, nowMs) },
    { key: "phase", at: activity.phase?.at ?? null, text: phaseText(activity) },
    // Once the writer leads, a turn can no longer be the now-line: it would say
    // the run is searching under a line saying it is writing.
    { key: "turn", at: activity.turn?.at ?? null, text: writerLeads
        ? null
        : turnText(activity, nowMs, true) },
  ];
  return latestActivity(candidates);
}

function latestActivity(
  candidates: Array<{ key: string; at: number | null; text: string | null }>,
): { key: string; at: number | null; text: string } | null {
  let best: { key: string; at: number | null; text: string } | null = null;
  for (const candidate of candidates) {
    if (candidate.text === null) continue;
    const row = { key: candidate.key, at: candidate.at, text: candidate.text };
    if (best === null) {
      best = row;
    } else if ((row.at ?? Number.NEGATIVE_INFINITY) > (best.at ?? Number.NEGATIVE_INFINITY)) {
      best = row;
    }
  }
  return best;
}

/** The heartbeat lines for a run in flight. Every string traces to one event. */
export function heartbeatReading(activity: DeepActivity, nowMs: number): HeartbeatReading {
  const writerLeads = writerLegLeads(activity);
  const best = pickNow(activity, nowMs, writerLeads);
  if (best === null) return { now: null, turn: null, subquestion: null };
  // The turn line already carries its own duration; everything else gets the
  // ticking age of the event that produced it.
  const now = best.key === "turn" ? best.text : withAge(best.text, best.at, nowMs);
  if (writerLeads) {
    // The research leg is over: report what it produced, and drop the
    // subquestion — it names an angle the run has finished with.
    return { now, turn: researchDoneLine(activity), subquestion: null };
  }
  return {
    now,
    turn: best.key === "turn" ? null : turnText(activity, nowMs, false),
    subquestion: activity.turn?.payload.subquestion ?? null,
  };
}

/** Seconds until the loop said it will retry, or null when it did not say. */
export function holdRemainingSeconds(
  resumeAtMs: number | null,
  nowMs: number,
): number | null {
  if (resumeAtMs === null) return null;
  return Math.max(0, Math.round((resumeAtMs - nowMs) / 1000));
}
