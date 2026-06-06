/**
 * Deep Research orchestrator hook (parallel to useBuild + useResearch):
 * creates a deep_research conversation, drives its run through the agent
 * loop's plan-mode flow, exposes the derived state. Components consume
 * only this.
 */

import { useCallback, useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  createDeepResearchConversation,
  exportReportAsMarkdown,
} from "@/api/deepResearch";
import {
  useDeepResearchStream,
  type DeepResearchSession,
} from "./useDeepResearchStream";

/** Same opt-in to the resume path the Build hook uses for /build/:cid. The
 *  /deep/:cid route passes the cid + a placeholder query; the WS subscription
 *  replays the conversation history (history-then-live) so the surface
 *  recovers all the derived state. */
export function useDeepResearch(resumeCid?: string | null) {
  const [session, setSession] = useState<DeepResearchSession | null>(null);
  const [leaderId, setLeaderId] = useState<string | null>(null);
  const [depthTier, setDepthTier] = useState<
    "quick" | "standard_deep" | "exhaustive"
  >("standard_deep");
  const stream = useDeepResearchStream(session);

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
          onSuccess: (cid) =>
            setSession({ cid, query: trimmed, depthTier }),
        },
      );
    },
    [create, leaderId, depthTier],
  );

  const reset = useCallback(() => setSession(null), []);

  const exportReport = useCallback(() => {
    if (stream.report) exportReportAsMarkdown(stream.report);
  }, [stream.report]);

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
    reset,
    exportReport,
    ...stream,
  };
}
