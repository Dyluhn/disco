/**
 * Deep Research session state, split out of `useDeepResearch`
 * (PKG-12-C/TS-0043/TS-0044): the surface's controls (leader/sources/depth/
 * recency), the active session, the resume-path effect, and the
 * G1/DR-4 pre-created-cid machinery so file attachments work before the
 * first submit.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import { useMutation } from "@tanstack/react-query";
import { createDeepResearchConversation } from "@/api/deepResearch";
import { agentLive } from "@/api/client";
import type { DeepResearchSession } from "../useDeepResearchStream";

export type Tier = "quick" | "standard_deep" | "exhaustive";

export interface DeepResearchSessionApi {
  session: DeepResearchSession | null;
  setSession: (session: DeepResearchSession | null) => void;
  leaderId: string | null;
  setLeaderId: (id: string | null) => void;
  sources: string[];
  setSources: (sources: string[]) => void;
  depthTier: Tier;
  setDepthTier: (tier: Tier) => void;
  recencyWindow: "month" | "week" | null;
  setRecencyWindow: (window: "month" | "week" | null) => void;
  /** G1/DR-4: the pre-created cid for the empty state (before first submit). */
  preCid: string | null;
  setPreCid: (cid: string | null) => void;
  preCidRef: MutableRefObject<string | null>;
  preCreateFlightRef: MutableRefObject<Promise<string> | null>;
  /** W-07: lazily obtain the pre-created cid for the Attach affordance so
   *  uploads work BEFORE the user submits. Returns the existing preCid, or
   *  creates one NOW carrying the CURRENT depth/recency/leader
   *  settings (so the upload lands in the conversation that will actually
   *  run) and stores it so submit() reuses the SAME cid. null offline. */
  ensurePreCid: () => Promise<string | null>;
}

export function useDeepResearchSession(
  resumeCid: string | null | undefined,
  initialLeaderId: string | null | undefined,
  initialSources: string[],
): DeepResearchSessionApi {
  // No localStorage auto-restore: a fresh surface starts EMPTY (compose). An
  // existing run is reached as a server resource via History (/deep/:cid), not a
  // client stash — so selecting the scope can never re-attach/trap you.
  const [session, setSession] = useState<DeepResearchSession | null>(null);
  // fix-c #2: seed from the parent ResearchSurface's leader pick so a
  // search → deep-research switch carries the model the user just chose
  // (otherwise the leader pill reset to the default and the submit re-routed
  // the run to a different model silently).
  const [leaderId, setLeaderId] = useState<string | null>(initialLeaderId ?? null);
  const [sources, setSources] = useState<string[]>(initialSources);
  const [depthTier, setDepthTier] = useState<Tier>("standard_deep");
  // DR-3: recency filter — null = off (any time), "month"/"week" = date-bounded.
  const [recencyWindow, setRecencyWindow] = useState<"month" | "week" | null>(null);

  // G1/DR-4: upload cid for the empty state. Creation is LAZY: merely visiting
  // the composer must not persist an empty History row. The first attachment
  // calls ensurePreCid; a plain submit creates the real conversation directly.
  // If controls change after an attachment, submit patches the SAME cid before
  // kickoff so uploaded files are retained and the final settings win.
  const [preCid, setPreCid] = useState<string | null>(null);
  const preCidRef = useRef<string | null>(null);
  const preCreateFlightRef = useRef<Promise<string> | null>(null);
  const preCreate = useMutation({ mutationFn: createDeepResearchConversation });

  // Resume path: a /deep/:cid route hands us a cid → open it READ-ONLY (kick is
  // absent, so the stream subscribes + replays but never sends the query).
  useEffect(() => {
    if (resumeCid && (session === null || session.cid !== resumeCid)) {
      setSession({ cid: resumeCid, query: "(resumed)", depthTier });
    }
  }, [resumeCid, session, depthTier]);

  const ensurePreCid = useCallback(async () => {
    if (!agentLive()) return null;
    const existing = preCidRef.current ?? preCid;
    if (existing) return existing;
    if (preCreateFlightRef.current) return preCreateFlightRef.current;
    const flight = preCreate.mutateAsync({
      query: "",
      leaderId,
      depthTier,
      recencyWindow,
      sources,
    }).then((cid) => {
      preCidRef.current = cid;
      setPreCid(cid);
      return cid;
    });
    preCreateFlightRef.current = flight;
    try {
      return await flight;
    } finally {
      if (preCreateFlightRef.current === flight) preCreateFlightRef.current = null;
    }
  }, [
    preCid,
    preCreate,
    depthTier,
    recencyWindow,
    leaderId,
    sources,
  ]);

  return {
    session,
    setSession,
    leaderId,
    setLeaderId,
    sources,
    setSources,
    depthTier,
    setDepthTier,
    recencyWindow,
    setRecencyWindow,
    preCid,
    setPreCid,
    preCidRef,
    preCreateFlightRef,
    ensurePreCid,
  };
}
