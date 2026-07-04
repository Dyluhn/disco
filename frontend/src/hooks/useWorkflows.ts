import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { approveWorkflow, listWorkflowReviews } from "@/api/workflows";
import type { WorkflowListResponse } from "@/types/workflow";

export const WORKFLOWS_KEY = ["workflows"] as const;

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
