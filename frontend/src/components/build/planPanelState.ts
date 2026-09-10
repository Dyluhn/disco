import type { ConversationStatus } from "@/types/agent";

export function defaultPlanPanelExpanded({
  gate,
  status,
}: {
  gate: boolean;
  status?: ConversationStatus;
}): boolean {
  return gate || status === "AWAITING_PLAN_APPROVAL";
}
