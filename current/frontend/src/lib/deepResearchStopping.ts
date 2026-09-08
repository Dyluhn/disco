/**
 * Stopping COPY — the words the run UI uses between the moment the user presses
 * Stop and the moment the run answers with its checkpoint.
 *
 * That gap is real and it is not short: the engine honours Stop at the next
 * boundary it reaches, and at the exhaustive tier one writer pass can be
 * several minutes long. Before this module the gap did not exist in the UI at
 * all — the server published a terminal IDLE 29 ms after Stop, every run
 * control disappeared, and the engine went on working for another eight
 * minutes behind a screen that said the run was over.
 *
 * So stopping is a wall like any other, and it owes the person in front of it
 * the same four things: WHY it is up, what the run is doing RIGHT NOW, what
 * happens next without anyone touching anything, and what is still allowed
 * meanwhile. Every one of them is built from an event the engine actually
 * emitted — the Stop marker's own timestamp, the newest heartbeat and its age,
 * the observed source count. Nothing here is a timer, an estimate, or a
 * prediction of when the run will land: the run has not told us, so we do not
 * say.
 */
import {
  formatDuration,
  heartbeatReading,
  modelIsBuffered,
  signalReading,
} from "@/lib/deepResearchHeartbeat";
import type { DeepActivity } from "@/lib/deepResearchTrace";

export interface StoppingCopy {
  /** Panel heading. */
  title: string;
  /** Why this wall is up: the user pressed Stop, at the instant recorded. */
  why: string;
  /** The step the run is in, from the newest reported fact, with its age. */
  stateNow: string;
  /** The age of the newest event of ANY kind — "Live" / "quiet for 35 s" /
   *  "no signal for 1:12", the same reading the strip shows. */
  signal: string;
  /** What happens on its own once the step in flight finishes. */
  next: string;
  /** What the user may do meanwhile, and what the one destructive option does. */
  allowed: string;
}

/** Local wall-clock time of an ISO instant, or null when it will not parse.
 *  A Stop the browser cannot date renders without a time rather than with a
 *  guessed one. */
function clockTime(iso: string): string | null {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  return new Date(ms).toLocaleTimeString();
}

function whyLine(requestedAt: string): string {
  const at = clockTime(requestedAt);
  return at === null ? "You pressed Stop." : `You pressed Stop at ${at}.`;
}

/** The step in flight, in the same vocabulary the heartbeat uses — one source
 *  of truth for "what is happening now", so the wall and the strip above it can
 *  never describe different steps. */
function stateLine(activity: DeepActivity, nowMs: number): string {
  const reading = heartbeatReading(activity, nowMs);
  if (reading.now === null) {
    return "The run has not reported a step yet, so there is nothing to name here.";
  }
  return reading.turn === null ? reading.now : `${reading.now} · ${reading.turn}`;
}

/** The signal reading, or null before any event carried a timestamp (then no
 *  honest age exists and none is shown). */
function signalLine(activity: DeepActivity, nowMs: number): string | null {
  if (activity.lastEventAt === null) return null;
  const signal = signalReading(
    activity.lastEventAt,
    nowMs,
    activity.lastEventAt,
    modelIsBuffered(activity),
  );
  return signal.level === "live"
    ? "Still reporting"
    : `${signal.label[0].toUpperCase()}${signal.label.slice(1)}`;
}

function nextLine(sources: number): string {
  const kept =
    sources > 0
      ? `the ${sources} source${sources === 1 ? "" : "s"} it has found are kept`
      : "whatever it has found is kept";
  return (
    `The run closes its current work and writes a checkpoint; ` +
    `${kept}, and Resume continues from them. No report is written from a ` +
    `stopped run — a report is published whole or not at all.`
  );
}

const ALLOWED =
  "Wait for the step to finish, or Kill — Kill ends the run immediately with " +
  "no checkpoint, so there is nothing left to resume.";

/** Every user-facing string for the stopping state, at one instant.
 *
 * `sourcesDiscovered` is the strip's own observed count (the sum of the
 * admitted-source observations), so the number promised here is the number
 * already on screen. */
export function stoppingCopy(
  activity: DeepActivity,
  nowMs: number,
  sourcesDiscovered: number,
): StoppingCopy | null {
  const marker = activity.stopRequested;
  if (marker === null) return null;
  return {
    title: "Stopping",
    why: whyLine(marker.payload.requested_at),
    stateNow: stateLine(activity, nowMs),
    signal: signalLine(activity, nowMs) ?? "",
    next: nextLine(Math.max(0, sourcesDiscovered)),
    allowed: ALLOWED,
  };
}

export interface KilledCopy {
  title: string;
  /** Why the run ended: the user chose it. Never a failure. */
  why: string;
  /** What exists now, and what does not. */
  stateNow: string;
  /** The only way forward from here. */
  next: string;
}

/**
 * The terminal a KILLED run gets — the other user-initiated ending.
 *
 * Kill cancels the run's task outright, so unlike Stop there is no boundary at
 * which the engine can write a checkpoint, and there is nothing to resume. That
 * is a real difference and the copy states it rather than dressing a kill up as
 * a pause. It is also not an error: nothing failed, a person decided.
 */
export function killedCopy(sourcesDiscovered: number): KilledCopy {
  const found = Math.max(0, sourcesDiscovered);
  const kept =
    found > 0
      ? `the ${found} source${found === 1 ? "" : "s"} it had found and the trace of what it did stay on this conversation`
      : "the trace of what it did stays on this conversation";
  return {
    title: "Run killed",
    why: "You killed this run. Nothing failed — it ended because you ended it.",
    stateNow: `It stopped where it was, so no checkpoint and no report were written; ${kept}.`,
    next: "Ask the question again to start a fresh run. Stop, rather than Kill, ends a run at its next boundary and leaves a checkpoint you can resume.",
  };
}

/** How long the run has been stopping, as the strip would say it — used for
 *  the one line that ages: "asked 1:40 ago". Null when the marker's own
 *  timestamp is missing, because then the span is unknown. */
export function stoppingForLabel(
  activity: DeepActivity,
  nowMs: number,
): string | null {
  const at = activity.stopRequested?.at ?? null;
  if (at === null) return null;
  const seconds = Math.max(0, Math.floor((nowMs - at) / 1000));
  return seconds < 1 ? null : `asked ${formatDuration(seconds)} ago`;
}
