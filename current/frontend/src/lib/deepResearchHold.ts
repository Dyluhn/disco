/**
 * Hold COPY (L24) — the words the run UI uses when the loop is holding because
 * the search pool is cooling.
 *
 * A hold is not a failure and must never be dressed as one. It is a wall the
 * system owns, so it has to radiate all four things a wall owes the person in
 * front of it: WHY it is up, what state the run is in NOW, what happens next
 * without anyone touching it, and what is still allowed meanwhile. Each of
 * those is a separate field below so no state can quietly ship without one.
 */
import { formatDuration, holdRemainingSeconds } from "@/lib/deepResearchHeartbeat";
import type { ActiveHold } from "@/lib/deepResearchTrace";

export interface HoldCopy {
  /** Panel heading. */
  title: string;
  /** Why the loop is holding, naming the engines the backend named. */
  why: string;
  /** The reassurance that this is infrastructure, not a failed run. */
  notAnError: string;
  /** What is true right now: turn, sources kept, queries queued. */
  stateNow: string;
  /**
   * The queued queries themselves — the work the hold is waiting to do. The
   * state line only counts them; the trace row that used to carry the text
   * truncates, so the panel renders the list in full.
   */
  queuedWork: string[];
  /** What happens by itself, with the countdown the engines promised. */
  next: string;
  /** What stopping costs — i.e. what remains allowed. */
  allowed: string;
  /** The one-line form used once the user has chosen to keep waiting. */
  collapsed: string;
}

function joinNames(names: string[]): string {
  if (names.length === 0) return "";
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function whyLine(hold: ActiveHold): string {
  const names = hold.payload.engines.map((engine) => engine.name);
  const list = joinNames(names);
  if (hold.payload.reason === "search_rate_starved") {
    return list
      ? `The search pool is out of request budget for the moment (${list}).`
      : "The search pool is out of request budget for the moment.";
  }
  if (!list) return "Every search engine in the pool is cooling down after hitting its rate limit.";
  return names.length === 1
    ? `${list} hit its rate limit and is cooling down.`
    : `${list} hit their rate limits and are cooling down.`;
}

function stateLine(hold: ActiveHold): string {
  const { turn, sources_retained: sources, queued_queries: queued } = hold.payload;
  const parts: string[] = [];
  if (turn.of > 0) parts.push(`Turn ${turn.n} of ${turn.of}`);
  parts.push(`${plural(sources, "source", "sources")} kept`);
  // F1: `queued_queries` is the LIST of queries the loop is holding on (the TS
  // mirror said `number`, so this clause could never render). The queries
  // themselves are the hold's trace-row detail.
  if (queued.length > 0) {
    parts.push(`${plural(queued.length, "query", "queries")} waiting to run`);
  }
  return parts.join(" · ");
}

function nextLine(remaining: number | null): string {
  if (remaining === null) {
    return "The engines did not say when they will be back. The run keeps re-checking on its own.";
  }
  if (remaining <= 0) return "Retrying now — waiting for the engines to answer.";
  return `Research resumes on its own in ${formatDuration(remaining)}.`;
}

function collapsedLine(remaining: number | null): string {
  if (remaining === null || remaining <= 0) return "Waiting for search engines — retrying now";
  return `Waiting for search engines — resumes in ${formatDuration(remaining)}`;
}

/** Every user-facing string for one hold, at one instant. */
export function holdCopy(hold: ActiveHold, nowMs: number): HoldCopy {
  const remaining = holdRemainingSeconds(hold.resumeAtMs, nowMs);
  const sources = hold.payload.sources_retained;
  return {
    title: "Waiting for search engines",
    why: whyLine(hold),
    notAnError:
      "This is a cooldown, not an error. The run is holding rather than firing searches into a pool that would return nothing.",
    stateNow: stateLine(hold),
    queuedWork: hold.payload.queued_queries,
    next: nextLine(remaining),
    allowed:
      sources > 0
        ? `Stopping keeps the ${plural(sources, "source", "sources")} found so far, and you can resume this run later.`
        : "Stopping ends the wait; you can resume this run later.",
    collapsed: collapsedLine(remaining),
  };
}

/** The trace row for a resumed hold: "Resumed after 4:02 — brave, mojeek live". */
export function holdResumedLabel(waitedSeconds: number, enginesLive: string[]): string {
  const head = `Resumed after ${formatDuration(waitedSeconds)}`;
  return enginesLive.length > 0 ? `${head} — ${enginesLive.join(", ")} live` : head;
}
