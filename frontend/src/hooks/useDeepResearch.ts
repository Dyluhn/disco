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

/** localStorage stash of the active deep-research session so the run survives
 *  navigation away from the surface (e.g. user pops over to Build then returns).
 *  Backend persistence isn't enough on its own — the cid lives in component
 *  state, and the WS gets torn down on unmount. Persisting the cid here lets
 *  the surface re-subscribe on remount; the WS history replay (already in
 *  place via subscribeConversation) hydrates all derived state. */
const ACTIVE_SESSION_KEY = "pmx.deep.activeSession";

function readActiveSession(): DeepResearchSession | null {
  try {
    const raw = window.localStorage.getItem(ACTIVE_SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as DeepResearchSession;
    if (typeof parsed?.cid !== "string") return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeActiveSession(s: DeepResearchSession | null) {
  try {
    if (s) window.localStorage.setItem(ACTIVE_SESSION_KEY, JSON.stringify(s));
    else window.localStorage.removeItem(ACTIVE_SESSION_KEY);
  } catch {
    /* swallow: localStorage may be unavailable (private mode) */
  }
}

/** Same opt-in to the resume path the Build hook uses for /build/:cid. The
 *  /deep/:cid route passes the cid + a placeholder query; the WS subscription
 *  replays the conversation history (history-then-live) so the surface
 *  recovers all the derived state. Without a route cid, we fall back to the
 *  localStorage stash so cross-surface navigation also resumes naturally. */
export function useDeepResearch(resumeCid?: string | null) {
  const [session, setSession] = useState<DeepResearchSession | null>(() =>
    resumeCid ? null : readActiveSession(),
  );
  const [leaderId, setLeaderId] = useState<string | null>(null);
  const [depthTier, setDepthTier] = useState<
    "quick" | "standard_deep" | "exhaustive"
  >(() => (readActiveSession()?.depthTier as
    | "quick"
    | "standard_deep"
    | "exhaustive") ?? "standard_deep");
  const stream = useDeepResearchStream(session);

  useEffect(() => {
    if (resumeCid && (session === null || session.cid !== resumeCid)) {
      setSession({ cid: resumeCid, query: "(resumed)", depthTier });
    }
  }, [resumeCid, session, depthTier]);

  // Mirror the active session to localStorage so navigating away + back
  // resumes. Clear the stash when the run reaches a terminal state so a stale
  // cid doesn't haunt the next visit.
  useEffect(() => {
    if (session && (stream.status === "FINISHED" || stream.status === "ERROR")) {
      writeActiveSession(null);
    } else {
      writeActiveSession(session);
    }
  }, [session, stream.status]);

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

  const reset = useCallback(() => {
    setSession(null);
    writeActiveSession(null);
  }, []);

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
