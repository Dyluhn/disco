import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getResearchHistory, requestResearch, type SavedSearchTurn } from "@/api/research";
import { agentLive } from "@/api/client";
import type { ReScope } from "@/types/grounded";
import { initialResearchStreamState, useResearchStream } from "./useResearchStream";
import { createBuildConversation } from "@/api/agent";

function restoredState(saved: SavedSearchTurn | undefined, scope: ReScope | null) {
  return saved ? {
    ...initialResearchStreamState, phase: "finished" as const,
    answer: saved.answer, blocks: saved.answer.blocks,
  } : {
    ...initialResearchStreamState,
    phase: scope ? "error" as const : "idle" as const,
    error: scope ? "This search did not finish. Retry explicitly to start a new request." : null,
  };
}

function parentRequest(scope: ReScope | null, hasAnswer: boolean) {
  return hasAnswer ? scope?.request_id ?? null : scope?.parent_request_id ?? null;
}

/** A restored answer is a read, never a subscription to the query endpoint. */
export function useResearch(resumeCid: string | null = null) {
  const qc = useQueryClient();
  const [liveScope, setLiveScope] = useState<ReScope | null>(null);
  const stream = useResearchStream(liveScope);
  const history = useQuery({
    queryKey: ["research-history", resumeCid],
    queryFn: () => getResearchHistory(resumeCid!),
    enabled: Boolean(resumeCid) && agentLive(),
    retry: false,
  });
  const [selected, setSelected] = useState<string | null>(null);
  const turns = history.data?.turns ?? [];
  const saved = turns.find((turn) => turn.scope.request_id === selected) ?? turns.at(-1);
  const scope = liveScope ?? saved?.scope ?? history.data?.latest_request?.scope ?? null;
  const parentId = parentRequest(scope, Boolean(liveScope ? stream.answer : saved?.answer));
  const [preCid, setPreCid] = useState<string | null>(resumeCid);
  const preCidRef = useRef<string | null>(resumeCid);
  const preCreateFlightRef = useRef<Promise<string> | null>(null);
  const preCreate = useMutation({ mutationFn: () => createBuildConversation(null, "research") });
  const ensurePreCid = useCallback(async () => {
    if (!agentLive()) return null;
    if (preCidRef.current) return preCidRef.current;
    if (preCreateFlightRef.current) return preCreateFlightRef.current;
    const flight = preCreate.mutateAsync().then((cid) => {
      preCidRef.current = cid;
      setPreCid(cid);
      return cid;
    });
    preCreateFlightRef.current = flight;
    try { return await flight; }
    finally { if (preCreateFlightRef.current === flight) preCreateFlightRef.current = null; }
  }, [preCreate]);

  const action = useMutation({
    mutationFn: async (next: ReScope) => {
      const cid = preCidRef.current ?? (agentLive() ? await ensurePreCid() : null);
      return requestResearch({ ...next, conversation_id: cid });
    },
    onSuccess: setLiveScope,
  });
  const submit = useCallback((query: string, opts?: {
    model_override?: string | null; think?: boolean; sources?: string[];
  }) => {
    action.mutate({ ...scope, query, ...opts, parent_request_id: parentId });
  }, [action, scope, parentId]);
  const reScope = useCallback((partial: Partial<ReScope>) => {
    action.mutate({ query: "", ...scope, ...partial, parent_request_id: parentId });
  }, [action, scope, parentId]);

  useEffect(() => {
    if (liveScope && stream.phase === "finished" && stream.answer) {
      void qc.invalidateQueries({ queryKey: ["conversations"] });
      void qc.invalidateQueries({ queryKey: ["research-history", preCid] });
    }
  }, [liveScope, stream.phase, stream.answer, qc, preCid]);

  const restored = restoredState(saved, scope);
  return {
    scope, submit, reScope,
    submitting: action.isPending || (liveScope !== null && stream.phase === "running"),
    reset: () => setLiveScope(null),
    preCid: scope === null ? preCid : null,
    ensurePreCid: agentLive() ? ensurePreCid : undefined,
    runCid: preCid,
    ...(liveScope ? stream : { ...restored, stop: stream.stop }),
    submitError: action.error,
    restoring: Boolean(resumeCid) && agentLive() && history.isPending,
    restoreError: history.error,
    retryRestore: () => history.refetch(),
    turns,
    selectTurn: (requestId: string) => { setSelected(requestId); setLiveScope(null); },
  };
}
export type ResearchController = ReturnType<typeof useResearch>;
