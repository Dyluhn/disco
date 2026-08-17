/**
 * Deep Research pure derivations, split out of `useDeepResearch`
 * (PKG-12-C/TS-0043/TS-0044): the recovered-query / follow-ups / effective-
 * query views computed from the stream on every render. No hook state is
 * needed for any of these, so they're plain functions composed by one thin
 * `useDeepResearchDerived` entry point (kept `use`-prefixed for parity with
 * the other three sub-hooks it's composed alongside in the parent).
 */
import type { MessageEvent } from "@/types/agent";
import type { DeepResearchSession, DeepResearchStream } from "../useDeepResearchStream";

// Recover the real query from replayed events (resume path).
// Priority: report.query (arrives with the finished report, most reliable) >
// first user MessageEvent (available from the very first replay frame, so the
// loading window is brief). The "(resumed)" sentinel in session.query is purely
// internal — it MUST NOT reach the UI.
function computeRecoveredQuery(stream: DeepResearchStream): string | null {
  return (
    stream.report?.query ??
    stream.events
      .filter((e): e is MessageEvent => e.kind === "message")
      .find((e) => e.message.role === "user")?.message.content ??
    null
  );
}

// Follow-up Q&A render (Wave 2 — the silent-drop fix). The RP-13 follow-up
// path appends the user's question + the agent's grounded answer as
// MessageEvents AFTER the report. DeepReportView only renders the ReportEvent,
// so these were generated server-side but NEVER shown (the answer vanished).
// We surface them here. DISPOSITION RULE (honors the whitelist boundary): only
// user + assistant messages render; environment/system plumbing
// (<system-reminder>, <reground-anchors>) is deliberately SUPPRESSED, not
// dumped. Anything past the report seq that isn't a clean Q/A is dropped.
//
// STALE-FOLLOW-UP FIX (WALK-08 A4): when no report exists yet, reportSeq
// would be -1 (the ?? -1 fallback), so the run's OWN initiating user message
// (seq ≥ 0 > -1) gets misclassified as a "follow-up".  Bail to [] whenever
// stream.report is null — a follow-up can't exist without a report.
function computeFollowUps(stream: DeepResearchStream): MessageEvent[] {
  return stream.report == null
    ? []
    : stream.events.filter(
        (e): e is MessageEvent =>
          e.kind === "message" &&
          (e.seq ?? 0) > (stream.report!.seq ?? -1) &&
          (e.message.role === "user" || e.message.role === "assistant") &&
          !(e.message.content ?? "").includes("<system-reminder>") &&
          !(e.message.content ?? "").includes("<reground-anchors>"),
      );
}

// The effective query for display: real query once recovered, a neutral loading
// string while mid-replay, or null when no session exists.
function computeEffectiveQuery(
  session: DeepResearchSession | null,
  recoveredQuery: string | null,
): string | null {
  return session === null
    ? null
    : recoveredQuery ??
        (session.query !== "(resumed)" ? session.query : "Resuming research…");
}

export interface DeepResearchDerivedApi {
  recoveredQuery: string | null;
  followUps: MessageEvent[];
  effectiveQuery: string | null;
}

export function useDeepResearchDerived(
  session: DeepResearchSession | null,
  stream: DeepResearchStream,
): DeepResearchDerivedApi {
  const recoveredQuery = computeRecoveredQuery(stream);
  const followUps = computeFollowUps(stream);
  const effectiveQuery = computeEffectiveQuery(session, recoveredQuery);
  return { recoveredQuery, followUps, effectiveQuery };
}
