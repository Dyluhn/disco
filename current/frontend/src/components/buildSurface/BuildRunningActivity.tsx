/**
 * The "normal" (non-ERROR, non-plan-gate) Activity feed body: the front-door
 * drafting-plan skeleton, the sticky plan/progress card, the collapsed-vs-full
 * activity feed, and the terminal final-message callout. Extracted verbatim
 * from BuildSurface.tsx's default branch (PKG-12-FE-BUILD).
 */

import { Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { Markdown } from "@/components/Markdown";
import { SANDBOX_URL_NOTE, scrubSandboxUrls } from "@/lib/sandboxUrls";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { AgentStageCard } from "@/components/build/AgentStageCard";
import { LiveSignalBar } from "@/components/build/LiveSignalBar";
import { PlanPanel } from "@/components/build/PlanPanel";
import type { ActivityItem, LiveSignal } from "@/lib/buildTrace";
import type { BuildController } from "./types";
import type { BuildFraming } from "@/components/BuildSurface";

export function BuildRunningActivity({
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
  return (
    <>
      {/* front-door: drafting the first plan (live, no plan yet) */}
      {draftingPlan && (
        <div
          aria-label="Drafting a plan"
          className="mb-section flex flex-col gap-hair rounded-control border border-hairline bg-surface-1 p-body"
        >
          <div className="flex items-center gap-hair font-ui text-[0.84rem] text-text-muted">
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
            Drafting a plan — exploring the workspace and shaping the steps…
          </div>
          <div className="mt-hair flex flex-col gap-hair" aria-hidden>
            <div className="h-2.5 w-2/3 animate-pulse rounded bg-surface-2" />
            <div className="h-2.5 w-5/6 animate-pulse rounded bg-surface-2" />
            <div className="h-2.5 w-1/2 animate-pulse rounded bg-surface-2" />
          </div>
        </div>
      )}
      {/* read-only capstone tracker: steps check off as the agent reports
          them. Sticky to the top of the scroll area so the plan + progress
          stay visible while the activity feed scrolls beneath it. */}
      {b.plan && (
        <div
          data-testid="build-sticky-plan-card"
          className="sticky top-0 z-20 -mx-body bg-bg px-body pb-section pt-px"
        >
          {/* runthru-v2 (#3): capable models that emit declarative progress
              snapshots get a live checklist; otherwise (small models, or none
              yet) the honest status chip — never a lying empty checklist. */}
          <PlanPanel
            plan={b.plan}
            progress={b.buildProgress.size ? b.buildProgress : undefined}
            status={b.status}
          />
        </div>
      )}
      {collapseFeed ? (
        // Quiet mode: one glanceable stage card (click-to-expand reveals the
        // full feed inline, preserving every inline artifact affordance).
        <AgentStageCard
          events={b.events}
          status={b.status}
          activity={activity}
          conversationId={b.cid ?? undefined}
          surface={framing}
        />
      ) : (
        <>
          <ActivityFeed items={activity} conversationId={b.cid ?? undefined} />
          <div className="mt-inline">
            <LiveSignalBar signal={liveSignal} />
          </div>
        </>
      )}
      {finalMessage &&
        (b.status === "FINISHED" ||
          b.status === "STUCK" ||
          b.status === "AWAITING_USER_DECISION") && (
          <FinalMessageCallout status={b.status} finalMessage={finalMessage} />
        )}
    </>
  );
}

/**
 * The terminal summary callout. UI-10: the agent narrates from INSIDE its sandbox
 * ("served via preview at http://localhost:8000/"), and that loopback address does
 * nothing in the user's browser. The rewrite happens HERE, at the presentation
 * layer — the stored transcript and the replay stream keep the model's own words.
 */
function FinalMessageCallout({
  status,
  finalMessage,
}: {
  status: BuildController["status"];
  finalMessage: string;
}) {
  const summary = scrubSandboxUrls(finalMessage);
  return (
    <div
      data-testid="build-final-summary"
      className={cn(
        "mt-section rounded-card border px-body py-inline text-[0.95rem]",
        status === "STUCK" ? "border-warn/40 bg-warn/5" : "border-hairline bg-surface-1",
      )}
    >
      {status === "STUCK" && (
        <p className="mb-hair font-ui text-[0.74rem] uppercase tracking-wide text-warn">
          Agent's latest reply
        </p>
      )}
      <Markdown>{summary.text}</Markdown>
      {summary.replaced && (
        <p
          data-disco-note="sandbox-url-rewritten"
          className="mt-inline font-ui text-[0.74rem] text-text-faint"
        >
          {SANDBOX_URL_NOTE}
        </p>
      )}
    </div>
  );
}
