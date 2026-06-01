import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAssignments, listModels, updateAssignments } from "@/api/models";
import type { AssignmentsPatch, ModelAssignments, ModelInfo } from "@/types/models";

/**
 * Query/mutation hooks for the model catalogue + assignments. Components consume
 * these — never the api layer directly (data-flow discipline). Query keys are
 * stable so the leader pill (Prompt 3) and the Settings matrix (Prompt 4) share
 * one cache.
 */

const MODELS_KEY = ["models"] as const;
const ASSIGNMENTS_KEY = ["model-assignments"] as const;

export function useModels() {
  return useQuery<ModelInfo[]>({ queryKey: MODELS_KEY, queryFn: listModels });
}

export function useAssignments() {
  return useQuery<ModelAssignments>({ queryKey: ASSIGNMENTS_KEY, queryFn: getAssignments });
}

export function useUpdateAssignments() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: AssignmentsPatch) => updateAssignments(patch),
    onSuccess: (next) => {
      // Write the authoritative result straight into the cache (no refetch flash).
      qc.setQueryData(ASSIGNMENTS_KEY, next);
    },
  });
}

/** Convenience: look a model up by id from a fetched catalogue. */
export function findModel(models: ModelInfo[] | undefined, id: string | null): ModelInfo | null {
  if (!models || !id) return null;
  return models.find((m) => m.id === id) ?? null;
}
