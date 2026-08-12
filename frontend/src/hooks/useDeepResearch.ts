/**
 * Deep Research orchestrator hook (parallel to useBuild + useResearch):
 * creates a deep_research conversation, drives its run through the agent
 * loop's plan-mode flow, exposes the derived state + lifecycle controls.
 *
 * LIFECYCLE MODEL (universal background-task pattern):
 *   - The run is a SERVER-SIDE resource (cid + persisted event log). It survives
 *     navigation away; closing the surface never kills it.
 *   - VIEW (open from History / a /deep/:cid route) is a SAFE read: subscribe +
 *     replay only — it NEVER starts the run (Command–Query Separation). No
 *     localStorage stash; selecting the deep-research scope = a fresh compose.
 *   - START is the only path that kicks (a fresh `submit()`).
 *   - CONTROL is explicit: stop (pause), kill (end), resume (continue), retry.
 *
 * Logical-line and complexity decomposition (PKG-12-C/TS-0043/TS-0044): the
 * session state (+ the G1/DR-4 pre-created-cid machinery), the run lifecycle
 * (create/submit/stop/kill/retry), the report export path, and the pure
 * derivations (recoveredQuery/followUps/effectiveQuery) each moved to their
 * own composed sub-hook under `./useDeepResearchParts/*`. This file's own
 * exported surface — the ONE recorded public declaration, `useDeepResearch`
 * itself — keeps its exact signature and composes the parts, returning the
 * same object (same keys, same order) the surface has always destructured.
 */

import { agentLive } from "@/api/client";
import { useDeepResearchStream } from "./useDeepResearchStream";
import { useDeepResearchDerived } from "./useDeepResearchParts/useDeepResearchDerived";
import { useDeepResearchExports } from "./useDeepResearchParts/useDeepResearchExports";
import { useDeepResearchLifecycle } from "./useDeepResearchParts/useDeepResearchLifecycle";
import { useDeepResearchSession } from "./useDeepResearchParts/useDeepResearchSession";

export function useDeepResearch(
  resumeCid?: string | null,
  initialLeaderId?: string | null,
  initialSources: string[] = [],
) {
  const sessionApi = useDeepResearchSession(resumeCid, initialLeaderId, initialSources);

  const stream = useDeepResearchStream(sessionApi.session);

  const derived = useDeepResearchDerived(sessionApi.session, stream);

  const lifecycle = useDeepResearchLifecycle({
    session: sessionApi.session,
    setSession: sessionApi.setSession,
    leaderId: sessionApi.leaderId,
    depthTier: sessionApi.depthTier,
    setDepthTier: sessionApi.setDepthTier,
    iterative: sessionApi.iterative,
    recencyWindow: sessionApi.recencyWindow,
    sources: sessionApi.sources,
    preCid: sessionApi.preCid,
    preCidRef: sessionApi.preCidRef,
    preCreateFlightRef: sessionApi.preCreateFlightRef,
    setPreCid: sessionApi.setPreCid,
    stream,
    recoveredQuery: derived.recoveredQuery,
  });

  const exportsApi = useDeepResearchExports(sessionApi.session);

  return {
    started: sessionApi.session !== null,
    cid: sessionApi.session?.cid ?? null,
    /** G1/DR-4: the pre-created cid for the empty state (before first submit).
     *  Pass to UploadComposer so text files can be attached before submitting.
     *  null when a session is already live or on the resume path. */
    preCid: sessionApi.session === null && !resumeCid ? sessionApi.preCid : null,
    /** W-07: lazily create-or-return the upload cid so Attach works pre-cid.
     *  Exposed only when a server exists (offline → Attach stays disabled). */
    ensurePreCid: agentLive() ? sessionApi.ensurePreCid : undefined,
    query: derived.effectiveQuery,
    /** True once the real query text is available (not the loading placeholder). */
    queryResolved:
      derived.recoveredQuery !== null ||
      (sessionApi.session !== null && sessionApi.session.query !== "(resumed)"),
    resumed: Boolean(resumeCid),
    submitting: lifecycle.create.isPending || lifecycle.startPrecreated.isPending,
    leaderId: sessionApi.leaderId,
    setLeaderId: sessionApi.setLeaderId,
    selectedSources: sessionApi.sources,
    setSelectedSources: sessionApi.setSources,
    depthTier: sessionApi.depthTier,
    setDepthTier: sessionApi.setDepthTier,
    iterative: sessionApi.iterative,
    setIterative: sessionApi.setIterative,
    recencyWindow: sessionApi.recencyWindow,
    setRecencyWindow: sessionApi.setRecencyWindow,
    submit: lifecycle.submit,
    runExhaustive: lifecycle.runExhaustive,
    stop: lifecycle.stop,
    kill: lifecycle.kill,
    retry: lifecycle.retry,
    reset: lifecycle.reset,
    /** Wave 2: post-report follow-up Q&A (user question + agent answer),
     *  in event order. Previously generated server-side but never rendered. */
    followUps: derived.followUps,
    exportReport: exportsApi.exportReport,
    exportReportByFmt: exportsApi.exportReportByFmt,
    /** Which server export (if any) is in flight. The surface disables both
     *  controls and spins the matching format until it settles. */
    exportPending: exportsApi.exportPending,
    ...stream,
    // A failed create was silent (empty state, no message). Expose it to the UI.
    submitError: lifecycle.create.error ?? lifecycle.startPrecreated.error ?? null,
  };
}
