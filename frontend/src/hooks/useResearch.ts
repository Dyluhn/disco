import { useCallback, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { requestResearch } from "@/api/research";
import type { ReScope } from "@/types/grounded";
import { useResearchStream } from "./useResearchStream";

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

  const submit = useCallback(
    (query: string, opts?: { model_override?: string | null; think?: boolean }) => {
      // The per-conversation lead-model override + Think flag ride along on the
      // request (backend hooks: CallContext.model_override + a think flag).
      action.mutate({ query, ...opts }, { onSuccess: setScope });
    },
    [action],
  );

  const reScope = useCallback(
    (partial: Partial<ReScope>) => {
      const next: ReScope = { query: scope?.query ?? "", ...scope, ...partial };
      action.mutate(next, { onSuccess: setScope });
    },
    [action, scope],
  );

  return {
    scope,
    submitting: action.isPending,
    submit,
    reScope,
    reset: () => setScope(null),
    ...stream,
    // Surface a failed submit/re-scope request (was silent: a failed mutation
    // left the user with an un-disabled button and no message). Distinct from the
    // stream `error` (which covers failures AFTER a run has started).
    submitError: action.error ?? null,
  };
}

export type ResearchController = ReturnType<typeof useResearch>;
