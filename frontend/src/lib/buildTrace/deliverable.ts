/**
 * Deliverable handoff — split out of `buildTrace.ts`.
 */
import type { AgentEvent } from "@/types/agent";
import type { DeliverableView } from "../buildTrace";

/** The latest finished-artifact handoff the agent declared via `serve` (a
 * DeliverableEvent). The newest wins — a later serve supersedes an earlier one
 * (the agent refined or replaced the deliverable). Returns null before any
 * handoff, so the panel stays hidden until there is a real thing to hand off
 * (no false affordance). */
export function deriveDeliverable(events: AgentEvent[]): DeliverableView | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "deliverable") {
      return {
        id: e.id,
        title: e.title,
        path: e.path,
        kind: e.artifact_kind,
        deploymentUrl: e.deployment_url || undefined,
      };
    }
  }
  return null;
}
