/**
 * DeepResearchRunView — the in-flight run machinery: the error state, the
 * planning loader, the plan-edit gate, the progress strip, and the
 * bounded-by notice. Relocated verbatim from DeepResearchSurface.tsx's
 * `started` JSX. See that file's header for the surface map.
 */
import { Loader2 } from "lucide-react";
import { PlanPanel } from "@/components/build/PlanPanel";
import { ErrorState } from "@/components/states";
import type { useDeepResearch } from "@/hooks/useDeepResearch";
import { DeepBoundedNotice } from "../DeepBoundedNotice";
import { DeepProgressStrip } from "../DeepProgressStrip";

interface Props {
  r: ReturnType<typeof useDeepResearch>;
}

export function DeepResearchRunView({ r }: Props) {
  return (
    <>
      {/* Error state */}
      {r.status === "ERROR" && (
        <ErrorState
          message={r.error ?? "The research run failed."}
          onRetry={r.retry}
        />
      )}

      {/* Planning loader — the gap between submit and the first PlanEvent
          arriving (typically 3-8 s). Without this the area below the H1 is
          completely blank, which looks like a hang. */}
      {!r.plan && r.status === "RUNNING" && (
        <div className="flex items-center gap-inline font-ui text-[0.86rem] text-text-muted">
          <Loader2 className="size-4 animate-spin text-accent" aria-hidden />
          Planning the research…
        </div>
      )}

      {/* Plan-edit gate — present only at AWAITING_PLAN_APPROVAL.
          Gap #52: PlanPanel is the SHARED build/DR plan gate (owned by the
          build lane) so its internal handles aren't surface-scoped here.
          Wrap the DR-side mount point with a stable marker so the harness can
          detect "the DR plan gate is showing" without reaching into PlanPanel. */}
      {r.awaitingPlan && r.plan && (
        <div data-dr-plan-gate="">
          <PlanPanel
            plan={{
              id: r.plan.id,
              summary: r.plan.summary,
              steps: r.plan.steps,
              revision: r.plan.revision,
              context: r.plan.context,
            }}
            progress={r.progress}
            onApprove={r.approvePlan}
            onRevise={r.requestPlan}
            // fix-c #1: the shared PlanPanel defaults its approve button to
            // "Approve & build" (build surface). On deep research the same
            // gate means something different — override.
            approveLabel="Approve research plan"
          />
        </div>
      )}

      {/* During the run + after: the progress strip (collapses on finish) */}
      {!r.awaitingPlan && r.plan && (
        <DeepProgressStrip
          plan={r.plan}
          progress={r.progress}
          trace={r.trace}
          stats={r.stats}
          status={r.status}
          followUpStatus={r.followUpStatus}
        />
      )}

      {/* The bounded-by honest notice — when the engine bounded out */}
      {r.report?.bounded_by && (
        <DeepBoundedNotice
          report={r.report}
          plan={r.plan}
          onTryExhaustive={
            r.report.depth_tier === "exhaustive"
              ? undefined
              : () => {
                  // fix-c #5: use the dedicated callback — set-then-submit
                  // closed over the OLD depthTier and re-ran at the same
                  // bounded tier. runExhaustive takes the tier directly.
                  if (r.query) r.runExhaustive(r.query);
                }
          }
        />
      )}
    </>
  );
}
