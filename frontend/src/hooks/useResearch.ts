import { useCallback, useState } from "react";
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

  // G1/DR-4 F1+F3: a lightweight conversation cid lets the server look up the
  // pre-attached upload corpus (keyed upload:{cid}) and keeps the run's artifacts
  // (sheet/slides) reachable via runCid. The cid is threaded onto the WS frame on
  // submit; the /ws/research endpoint is otherwise stateless.
  // BW-08 twin: pre-create is LAZY — it does NOT POST /conversations on mount.
  // The old eager mount-create minted a fresh server row (surface="agent") on
  // every page load of this (default) surface — 0-event "(untitled)" ghosts that
  // flooded History/Projects (mirrors the build-surface bug BW-08 fixed). The cid
  // is now created only on a REAL signal: an actual upload (ensurePreCid, called by
  // the paperclip) or submit (which mints + reuses one cid). Offline (no
  // VITE_AGENT_BASE) → no create ever fires (fixture mode), exactly as before.
  const [preCid, setPreCid] = useState<string | null>(null);
  const preCreate = useMutation({
    // "agent"-tagged minimal conversation record, no loop/sandbox.
    mutationFn: () => createBuildConversation(null, "agent"),
  });

  // W-07: lazily obtain the pre-created cid for the Attach affordance. Returns the
  // existing preCid, or creates one NOW (same surface as the eager mount path) and
  // stores it so the upcoming submit reuses it — routing the upload to THIS
  // (standard search) pipeline. null offline (no upload possible).
  const ensurePreCid = useCallback(async () => {
    if (!agentLive()) return null;
    if (preCid) return preCid;
    const cid = await preCreate.mutateAsync();
    setPreCid(cid);
    return cid;
  }, [preCid, preCreate]);

  const submit = useCallback(
    async (
      query: string,
      opts?: { model_override?: string | null; think?: boolean; space_ids?: string[] },
    ) => {
      // The per-conversation lead-model override + Think flag ride along on the
      // request (backend hooks: CallContext.model_override + a think flag).
      // BW-08 twin: reuse an upload's preCid if present; otherwise mint the cid NOW
      // (on this real submit) so the run's artifacts stay reachable via runCid.
      // Offline → null cid (fixture mode). The minted cid is stored so reScope /
      // runCid keep the run under ONE conversation.
      let cid = preCid;
      if (!cid && agentLive()) {
        cid = await preCreate.mutateAsync();
        setPreCid(cid);
      }
      action.mutate(
        { query, ...opts, conversation_id: cid ?? null },
        { onSuccess: setScope },
      );
    },
    [action, preCid, preCreate],
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
    /** W-07: lazily create-or-return the upload cid so Attach works pre-cid.
     *  Exposed only when a server exists — offline there's nothing to upload to,
     *  so Attach stays honestly disabled (no false affordance). */
    ensurePreCid: agentLive() ? ensurePreCid : undefined,
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
