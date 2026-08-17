/**
 * Dispatches the Activity feed's content between the three mutually-exclusive
 * top-level states: the recovery/retry ERROR view, the plan-approval gate, and
 * (the default) the running/settled activity — delegated to BuildRunningActivity
 * so its own branching doesn't accumulate here. Extracted verbatim from
 * BuildSurface.tsx's feed-content ternary chain (PKG-12-FE-BUILD).
 */

import { ErrorState } from "@/components/states";
import { BuildModelPicker } from "@/components/build/BuildModelPicker";
import { PlanPanel } from "@/components/build/PlanPanel";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { BuildRunningActivity } from "./BuildRunningActivity";
import type { ActivityItem, LiveSignal } from "@/lib/buildTrace";
import type { BuildController } from "./types";
import type { BuildFraming } from "@/components/BuildSurface";

export function BuildActivityFeedContent({
  b,
  activity,
  liveSignal,
  finalMessage,
  draftingPlan,
  collapseFeed,
  framing,
}: {
  b: BuildController;
  activity: ActivityItem[];
  liveSignal: LiveSignal;
  finalMessage: string | null;
  draftingPlan: boolean;
  collapseFeed: boolean;
  framing: BuildFraming;
}) {
  if (b.status === "ERROR") {
    // Recovery: "Try again" RESUMES the errored conversation — it re-kicks
    // the loop from the full persisted history (events + workspace snapshot
    // survive), it does NOT reset/discard progress. The backend flips
    // ERROR → RUNNING and continues; the existing WS subscription streams the
    // new events in over the preserved view. The model picker lets the user
    // switch to a DIFFERENT model before retrying (the failed model may BE the
    // cause); b.resume patches the pick first so the retry runs on the new one.
    return (
      <div className="flex flex-col gap-section">
        <ErrorState message={b.error ?? "The agent run failed."} onRetry={b.resume} />
        <div className="flex flex-col items-start gap-hair rounded-control border border-hairline bg-surface-1 p-body">
          <p className="font-ui text-[0.78rem] text-text-muted">
            Try a different model before retrying:
          </p>
          <BuildModelPicker value={b.modelId} onChange={b.setModelId} surface={framing} />
        </div>
      </div>
    );
  }
  if (b.awaitingPlan && b.plan) {
    // Plan gate is the hero: review + Approve/Revise before any work runs.
    return (
      <div className="flex flex-col gap-section">
        <PlanPanel plan={b.plan} onApprove={b.approvePlan} onRevise={b.requestPlan} />
        {activity.length > 0 && <ActivityFeed items={activity} conversationId={b.cid ?? undefined} />}
      </div>
    );
  }
  return (
    <BuildRunningActivity
      b={b}
      activity={activity}
      liveSignal={liveSignal}
      finalMessage={finalMessage}
      draftingPlan={draftingPlan}
      collapseFeed={collapseFeed}
      framing={framing}
    />
  );
}
