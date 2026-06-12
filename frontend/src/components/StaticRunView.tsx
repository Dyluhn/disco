/**
 * A read-only render of a finished run's event log — the shared renderer behind
 * both ShareView (a shared bundle by token) and ImportedRunView (a locally imported
 * bundle by cid). Pure: it derives all panels from the events via the same selectors
 * the live Build view uses, with ZERO WebSocket. `untrusted` hardens the preview
 * iframe (drops allow-scripts) for third-party content — always true for shared and
 * imported runs.
 */

import { useMemo } from "react";
import type { ReactNode } from "react";
import {
  deriveActivity,
  deriveDeliverable,
  derivePlan,
  derivePlanProgress,
  latestAgentMessage,
} from "@/lib/buildTrace";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { ExecutionCanvas } from "@/components/build/ExecutionCanvas";
import { PlanPanel } from "@/components/build/PlanPanel";
import { Markdown } from "@/components/Markdown";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

export function StaticRunView({
  events,
  status,
  banner,
  untrusted = true,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  /** The header row (provenance) — "shared on …" or "Imported — read-only". */
  banner: ReactNode;
  untrusted?: boolean;
}) {
  const activity = useMemo(() => deriveActivity(events, null, status), [events, status]);
  const plan = useMemo(() => derivePlan(events), [events]);
  const planProgress = useMemo(() => derivePlanProgress(events, status), [events, status]);
  const finalMessage = useMemo(() => latestAgentMessage(events), [events]);
  const deliverable = useMemo(() => deriveDeliverable(events), [events]);

  return (
    <div className="flex min-h-full flex-col">
      {banner}
      <div className="flex flex-1 min-h-0 lg:flex-row flex-col">
        {/* Left pane — Activity Feed + Plan */}
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-body py-inline lg:max-w-[45%]">
          {plan && (
            <div className="mb-section">
              <PlanPanel plan={plan} progress={planProgress} />
            </div>
          )}

          {activity.length === 0 && !plan && (
            <p className="font-ui text-[0.82rem] text-text-faint">
              This run has no activity to show.
            </p>
          )}

          {/* No cid → the ActivityFeed sheet-download / screenshot affordances are
              inert by construction (they only render with a conversationId). */}
          <ActivityFeed items={activity} />

          {finalMessage && (
            <div className="mt-section rounded-card border border-hairline bg-surface-1 px-body py-inline text-[0.95rem]">
              <Markdown>{finalMessage}</Markdown>
            </div>
          )}

          {deliverable && (
            <div className="mt-inline rounded-card border border-accent/30 bg-accent/5 px-body py-inline">
              <p className="font-ui text-[0.74rem] uppercase tracking-wide text-accent">
                Deliverable
              </p>
              <p className="mt-hair font-ui text-[0.84rem] text-text">{deliverable.title}</p>
              <p className="font-mono text-[0.7rem] text-text-faint">{deliverable.path}</p>
            </div>
          )}
        </div>

        {/* Right pane — Inspector (Files · Terminal · hardened Preview) */}
        <div className="min-h-0 flex-1 border-t border-hairline lg:border-l lg:border-t-0">
          <ExecutionCanvas events={events} status={status} cid={null} untrusted={untrusted} />
        </div>
      </div>
    </div>
  );
}
