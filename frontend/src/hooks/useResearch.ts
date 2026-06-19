import { useCallback, useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { requestResearch } from "@/api/research";
import { agentLive } from "@/api/client";
import type { ReScope } from "@/types/grounded";
import { useResearchStream } from "./useResearchStream";
import { createBuildConversation } from "@/api/agent";

/** Mutation hook (use[Action]) for submitting / re-scoping a research request.
 * Goes through the data-access layer (`requestResearch`), never transport. */
function useReScope() {
  return useMutation({ mutationFn: (scope: ReScope) => requestResearch(scope) });
}

/**
 * The orchestrator the Research surface consumes. Owns the active scope, drives
 * the stream, and exposes the user actions (submit, re-scope, stop). Components
 * touch only this — no API access in the component tree (BoD §13.8).
 */
export function useResearch() {
  const [scope, setScope] = useState<ReScope | null>(null);
  const stream = useResearchStream(scope);
  const action = useReScope();

  // G1/DR-4 F1+F3: pre-create a lightweight "research" conversation on mount so
  // the UploadComposer can render in the empty state (before the user submits).
  // On submit, the cid is threaded onto the WS frame so the server loads the
  // pre-attached upload passages and seeds them into the rerank step.
  // The /ws/research endpoint is stateless but the cid lets the server look up
  // the upload corpus keyed upload:{cid}.
  // OFF: when offline (no VITE_AGENT_BASE), no pre-create fires (fixture mode).
  const [preCid, setPreCid] = useState<string | null>(null);
  const preCreate = useMutation({
    // "research" surface — minimal conversation record, no loop/sandbox.
    mutationFn: () => createBuildConversation(null, "agent"),
  });

  useEffect(() => {
    if (scope !== null || !agentLive()) return; // started or offline: no pre-create
    if (preCid) return; // already created
    preCreate.mutate(undefined, { onSuccess: setPreCid });
  }, [scope]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = useCallback(
    (query: string, opts?: { model_override?: string | null; think?: boolean }) => {
      // The per-conversation lead-model override + Think flag ride along on the
      // request (backend hooks: CallContext.model_override + a think flag).
      // G1/DR-4: thread the pre-created cid so the server can load upload seeds.
      action.mutate(
        { query, ...opts, conversation_id: preCid ?? null },
        { onSuccess: setScope },
      );
    },
    [action, preCid],
  );

  const reScope = useCallback(
    (partial: Partial<ReScope>) => {
      const next: ReScope = { query: scope?.query ?? "", ...scope, ...partial };
      // D1: thread the same run cid so re-scoped results stay under ONE conversation
      // — its sheet/slides artifacts remain reachable (the download affordance stays
      // wired, never a 404).
      action.mutate({ ...next, conversation_id: preCid ?? null }, { onSuccess: setScope });
    },
    [action, scope, preCid],
  );

  return {
    scope,
    submitting: action.isPending,
    submit,
    reScope,
    reset: () => setScope(null),
    /** G1/DR-4: pre-created cid for the empty-state UploadComposer. null when a
     *  run is active (the scope is live) or offline (no server). */
    preCid: scope === null ? preCid : null,
    /** D1: the run's conversation id, RETAINED through the run (unlike preCid, which
     *  the empty-state gate hides). Threaded into AnswerDocument so in-block sheet /
     *  slides downloads (which fetch /conversations/{cid}/artifacts/…) appear ONLY
     *  when they can actually work. null offline → no false affordance. */
    runCid: preCid,
    ...stream,
    // Surface a failed submit/re-scope request (was silent: a failed mutation
    // left the user with an un-disabled button and no message). Distinct from the
    // stream `error` (which covers failures AFTER a run has started).
    submitError: action.error ?? null,
  };
}

export type ResearchController = ReturnType<typeof useResearch>;
