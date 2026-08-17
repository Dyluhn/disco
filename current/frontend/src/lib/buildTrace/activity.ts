/**
 * `deriveActivity` — turns the raw agent event stream into the plain-language
 * Activity Feed (Project-Manager tier, BoD §13.4). Walks events in order — the
 * activity feed is a chronological chat-and-action log, not an action-only
 * ledger. User messages (steer, send_message, revise) and mid-stream agent
 * prose replies (ask_user free-form questions) are first-class items alongside
 * tool calls. This is what makes typed input feel acknowledged: it appears in
 * the timeline the moment the optimistic event is dispatched, then the
 * server's canonical echo replaces the placeholder.
 *
 * The TRAILING agent message is the surface's "final answer" — rendered as a
 * Markdown panel elsewhere in the BuildSurface. Excluding it from the feed
 * prevents the same text rendering twice + keeps the feed the running
 * narrative.
 *
 * Split into `./activityLabels` (plain-language vocabulary),
 * `./activityObservations` (per-action-id observation/error indexing) and
 * `./activityItems` (one row-builder per event kind) so this file stays a
 * thin, low-branching dispatcher instead of one 79-branch function.
 */
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import {
  activityItemForAction,
  activityItemForAgentMessage,
  activityItemForAppkitEjection,
  activityItemForDeliverable,
  activityItemForEnvironmentMessage,
  activityItemForUserMessage,
  activityItemForWorkspaceRestored,
} from "./activityItems";
import type { ActivityBuildContext } from "./activityItems";
import {
  buildObservationIndex,
  collectLatestEditableByBase,
  collectObservedAndFailed,
  findLastAgentMessageIndex,
} from "./activityObservations";
import type { ActivityItem } from "../buildTrace";


export function deriveActivity(
  events: AgentEvent[],
  pendingActionId: string | null,
  status: ConversationStatus,
): ActivityItem[] {
  const { observed, failed } = collectObservedAndFailed(events);
  const lastAgentMessageIdx = findLastAgentMessageIndex(events);
  const latestEditableByBase = collectLatestEditableByBase(events);
  const observationByActionId = buildObservationIndex(events, latestEditableByBase);
  const ctx: ActivityBuildContext = {
    pendingActionId,
    status,
    observed,
    failed,
    observationByActionId,
  };
  const out: ActivityItem[] = [];
  for (let idx = 0; idx < events.length; idx++) {
    const e = events[idx];
    let item: ActivityItem | null = null;
    if (e.kind === "action") {
      item = activityItemForAction(e, ctx);
    } else if (e.kind === "message" && e.source === "user") {
      item = activityItemForUserMessage(e);
    } else if (e.kind === "message" && e.source === "agent") {
      item = activityItemForAgentMessage(e, idx, lastAgentMessageIdx);
    } else if (e.kind === "message" && e.source === "environment") {
      item = activityItemForEnvironmentMessage(e);
    } else if (e.kind === "deliverable") {
      item = activityItemForDeliverable(e);
    } else if (e.kind === "appkit_ejection") {
      item = activityItemForAppkitEjection(e);
    } else if (e.kind === "workspace_restored") {
      item = activityItemForWorkspaceRestored(e);
    }
    if (item) out.push(item);
  }
  return out;
}
