import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  approveWorkflow,
  authorWorkflow,
  draftWorkflowFromDescription,
  getAuthoringContext,
  listWorkflowReviews,
  runWorkflow,
  scheduleWorkflow,
} from "@/api/workflows";
import type { WorkflowListResponse } from "@/types/workflow";

export const WORKFLOWS_KEY = ["workflows"] as const;
export const WORKFLOW_AUTHORING_CONTEXT_KEY = ["workflows", "authoring-context"] as const;

export function useWorkflowReviews() {
  return useQuery<WorkflowListResponse>({
    queryKey: WORKFLOWS_KEY,
    queryFn: listWorkflowReviews,
  });
}

export function useApproveWorkflow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: approveWorkflow,
    onSuccess: (workflow) => {
      qc.setQueryData<WorkflowListResponse>(WORKFLOWS_KEY, (prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          workflows: prev.workflows.map((item) =>
            item.instance_id === workflow.instance_id ? workflow : item,
          ),
        };
      });
    },
  });
}

export function useAuthoringContext() {
  return useQuery({
    queryKey: WORKFLOW_AUTHORING_CONTEXT_KEY,
    queryFn: getAuthoringContext,
  });
}

export function useAuthorWorkflow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: authorWorkflow,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: WORKFLOWS_KEY });
    },
  });
}

export function useDraftFromDescription() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: draftWorkflowFromDescription,
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: WORKFLOWS_KEY });
    },
  });
}

export function useRunWorkflow() {
  return useMutation({
    mutationFn: runWorkflow,
  });
}

export function useScheduleWorkflow() {
  return useMutation({
    mutationFn: scheduleWorkflow,
  });
}
