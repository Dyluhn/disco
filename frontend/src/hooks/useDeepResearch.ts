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
 */

import { useCallback, useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  createDeepResearchConversation,
  exportReportAsMarkdown,
  exportReport,
  type ReportExportFmt,
} from "@/api/deepResearch";
import { killConversation } from "@/api/agent";
import {
  useDeepResearchStream,
  type DeepResearchSession,
} from "./useDeepResearchStream";

type Tier = "quick" | "standard_deep" | "exhaustive";

export function useDeepResearch(resumeCid?: string | null) {
  // No localStorage auto-restore: a fresh surface starts EMPTY (compose). An
  // existing run is reached as a server resource via History (/deep/:cid), not a
  // client stash — so selecting the scope can never re-attach/trap you.
  const [session, setSession] = useState<DeepResearchSession | null>(null);
  const [leaderId, setLeaderId] = useState<string | null>(null);
  const [depthTier, setDepthTier] = useState<Tier>("standard_deep");
  const stream = useDeepResearchStream(session);

  // Resume path: a /deep/:cid route hands us a cid → open it READ-ONLY (kick is
  // absent, so the stream subscribes + replays but never sends the query).
  useEffect(() => {
    if (resumeCid && (session === null || session.cid !== resumeCid)) {
      setSession({ cid: resumeCid, query: "(resumed)", depthTier });
    }
  }, [resumeCid, session, depthTier]);

  const create = useMutation({ mutationFn: createDeepResearchConversation });

  const submit = useCallback(
    (query: string) => {
      const trimmed = query.trim();
      if (!trimmed) return;
      create.mutate(
        { query: trimmed, leaderId, depthTier },
        {
          // kick:true — this is the ONLY path that starts the run.
          onSuccess: (cid) =>
            setSession({ cid, query: trimmed, depthTier, kick: true }),
        },
      );
    },
    [create, leaderId, depthTier],
  );

  // Stop = pause (cooperative; the engine halts at the next checkpoint and keeps
  // the partial report). The Stop button maps to the stream's cancel.
  const stop = stream.cancel;

  // Kill = end the run for good (force-cancel the server task; final).
  const kill = useCallback(async () => {
    stream.cancel();
    if (session) await killConversation(session.cid);
  }, [session, stream]);

  // Resume = continue a stopped/incomplete run (explicit; never on open).
  const resume = stream.resume;

  // Retry = a fresh run of the same query (a NEW conversation).
  const retry = useCallback(() => {
    if (session?.query && session.query !== "(resumed)") {
      setSession(null);
      submit(session.query);
    }
  }, [session, submit]);

  const reset = useCallback(() => setSession(null), []);

  const exportMd = useCallback(() => {
    if (stream.report) exportReportAsMarkdown(stream.report);
  }, [stream.report]);

  const exportReportByFmt = useCallback(
    async (fmt: ReportExportFmt) => {
      if (!session?.cid) return;
      if (fmt === "md") {
        exportMd();
        return;
      }
      // Server-side export for pdf/docx. The UI reports errors via toast/surface.
      await exportReport(session.cid, fmt);
    },
    [session?.cid, exportMd],
  );

  // Legacy export (single-button MD download) — kept for backward compat.
  const exportReport = exportMd;

  return {
    started: session !== null,
    cid: session?.cid ?? null,
    query: session?.query ?? null,
    resumed: Boolean(resumeCid),
    submitting: create.isPending,
    leaderId,
    setLeaderId,
    depthTier,
    setDepthTier,
    submit,
    stop,
    kill,
    resume,
    retry,
    reset,
    exportReport,
    exportReportByFmt,
    ...stream,
    // A failed create was silent (empty state, no message). Expose it to the UI.
    submitError: create.error ?? null,
  };
}
