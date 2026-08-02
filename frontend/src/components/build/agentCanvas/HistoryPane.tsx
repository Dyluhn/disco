import { useMemo } from "react";
import { deriveActivity } from "@/lib/buildTrace";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import { Empty } from "./Empty";

/** W-43 — Agent History: the FULL ActivityFeed surfaced as an inspector tab, so the
 * complete narrative (and every inline artifact/download/screenshot/deck-export
 * affordance it carries) stays reachable even when the left chat pane is collapsed to
 * the stage card (Verbose Agent Chat OFF). Derived from the same event stream. */
export function HistoryPane({
  events,
  status,
  cid,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
}) {
  // No pending-confirmation context in the inspector → pass null; the gate panels
  // live on the chat pane, this is the read-only chronological log.
  const activity = useMemo(() => deriveActivity(events, null, status), [events, status]);
  if (activity.length === 0)
    return <Empty>The agent's step-by-step history — messages, tool calls, and outputs — shows here.</Empty>;
  return (
    <div className="h-full overflow-auto px-body py-inline">
      <ActivityFeed items={activity} conversationId={cid ?? undefined} />
    </div>
  );
}
