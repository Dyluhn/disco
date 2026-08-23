/**
 * Deep Research run lifecycle, split out of `useDeepResearch`
 * (PKG-12-C/TS-0043/TS-0044): create / start / submit / runExhaustive / stop /
 * kill / retry / reset — the universal background-task control surface
 * described in the parent file's header.
 */
import { useCallback } from "react";
import type { MutableRefObject } from "react";
import { useMutation } from "@tanstack/react-query";
import { createDeepResearchConversation } from "@/api/deepResearch";
import { killConversation, patchConversationSettings } from "@/api/agent";
import { markConversationKilled } from "@/lib/sessionResume";
import type { DeepResearchSession, DeepResearchStream } from "../useDeepResearchStream";
import type { Tier } from "./useDeepResearchSession";

export interface DeepResearchLifecycleParams {
  session: DeepResearchSession | null;
  setSession: (session: DeepResearchSession | null) => void;
  leaderId: string | null;
  depthTier: Tier;
  setDepthTier: (tier: Tier) => void;
  recencyWindow: "month" | "week" | null;
  sources: string[];
  preCid: string | null;
  preCidRef: MutableRefObject<string | null>;
  preCreateFlightRef: MutableRefObject<Promise<string> | null>;
  setPreCid: (cid: string | null) => void;
  stream: DeepResearchStream;
  /** The recovered real query (resume path), computed by
   *  `useDeepResearchDerived` — retry() falls back to it when
   *  `session.query` is still the "(resumed)" sentinel. */
  recoveredQuery: string | null;
}

export function useDeepResearchLifecycle(params: DeepResearchLifecycleParams) {
  const {
    session,
    setSession,
    leaderId,
    depthTier,
    setDepthTier,
    recencyWindow,
    sources,
    preCid,
    preCidRef,
    preCreateFlightRef,
    setPreCid,
    stream,
    recoveredQuery,
  } = params;

  const create = useMutation({ mutationFn: createDeepResearchConversation });

  const startPrecreated = useMutation({
    mutationFn: async ({
      targetCid,
      query,
      modelOverride,
      runDepthTier,
      runRecencyWindow,
      runSources,
    }: {
      targetCid: string | Promise<string>;
      query: string;
      modelOverride: string | null;
      runDepthTier: Tier;
      runRecencyWindow: "month" | "week" | null;
      runSources: string[];
    }) => {
      const cid = await targetCid;
      await patchConversationSettings(cid, {
        modelOverride,
        depthTier: runDepthTier,
        recencyWindow: runRecencyWindow,
        sources: runSources,
      });
      return { cid, query, depthTier: runDepthTier, kick: true } as DeepResearchSession;
    },
    onSuccess: (nextSession) => {
      preCidRef.current = null;
      setPreCid(null);
      setSession(nextSession);
    },
  });

  const submit = useCallback(
    (query: string) => {
      const trimmed = query.trim();
      if (!trimmed) return;
      // G1/DR-4: if we pre-created a cid (for upload support in the empty state),
      // USE IT instead of creating a new one — so any uploaded files are already
      // associated with the conversation that will run. Current settings are
      // patched immediately before kickoff. An in-flight attachment create is
      // shared rather than forking a second conversation.
      const uploadCid = preCidRef.current ?? preCid ?? preCreateFlightRef.current;
      if (uploadCid) {
        startPrecreated.mutate({
          targetCid: uploadCid,
          query: trimmed,
          modelOverride: leaderId,
          runDepthTier: depthTier,
          runRecencyWindow: recencyWindow,
          runSources: sources,
        });
        return;
      }
      create.mutate(
        { query: trimmed, leaderId, depthTier, recencyWindow, sources },
        {
          // kick:true — this is the ONLY path that starts the run.
          onSuccess: (cid) =>
            setSession({ cid, query: trimmed, depthTier, kick: true }),
        },
      );
    },
    [
      create,
      startPrecreated,
      leaderId,
      depthTier,
      recencyWindow,
      sources,
      preCid,
      preCidRef,
      preCreateFlightRef,
      setSession,
    ],
  );

  // fix-c #5: the bounded-by "Run on exhaustive tier" button used to call
  // setDepthTier("exhaustive") then submit() in the same tick — but submit is
  // a useCallback closed over the OLD depthTier, so the POST raced the state
  // update and re-ran at the same bounded tier. This callback takes the tier
  // as a direct argument so it never closes over depthTier.
  const runExhaustive = useCallback(
    (query: string) => {
      const q = query.trim();
      if (!q) return;
      setDepthTier("exhaustive");
      create.mutate(
        {
          query: q,
          leaderId,
          depthTier: "exhaustive",
          recencyWindow,
          sources,
        },
        {
          onSuccess: (cid) =>
            setSession({ cid, query: q, depthTier: "exhaustive", kick: true }),
        },
      );
    },
    [create, leaderId, recencyWindow, sources, setDepthTier, setSession],
  );

  // Stop = pause (cooperative; the engine halts at the next checkpoint and keeps
  // the partial report). The Stop button maps to the stream's cancel.
  const stop = stream.cancel;

  // Kill = end the run for good (force-cancel the server task; final).
  const kill = useCallback(async () => {
    markConversationKilled(session?.cid);
    stream.cancel();
    if (session) await killConversation(session.cid);
  }, [session, stream]);

  // Retry = a fresh run of the same query (a NEW conversation). Use the recovered
  // query first (handles the resume path where session.query is the sentinel).
  const retry = useCallback(() => {
    const q =
      recoveredQuery ??
      (session?.query && session.query !== "(resumed)" ? session.query : null);
    if (q) {
      setSession(null);
      submit(q);
    }
  }, [recoveredQuery, session, submit, setSession]);

  const reset = useCallback(() => setSession(null), [setSession]);

  return { create, startPrecreated, submit, runExhaustive, stop, kill, retry, reset };
}
