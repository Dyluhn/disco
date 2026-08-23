/**
 * DeepResearchRunView — the in-flight run machinery: the error state, the
 * start-up loader, the progress strip, and the bounded-by notice.
 *
 * v2 (gateless deep research): the plan-edit gate is GONE from this surface.
 * Submitting starts the research, so there is nothing to approve or revise —
 * the strip mounts as soon as the run is live and the model's brief is the
 * first thing in it. See DeepResearchSurface.tsx's header for the surface map.
 */
import { Loader2 } from "lucide-react";
import { ErrorState } from "@/components/states";
import type { useDeepResearch } from "@/hooks/useDeepResearch";
import { DeepBoundedNotice } from "../DeepBoundedNotice";
import { DeepProgressStrip } from "../DeepProgressStrip";
import { DeepSteerInput } from "../DeepSteerInput";

interface Props {
  r: ReturnType<typeof useDeepResearch>;
}

export function DeepResearchRunView({ r }: Props) {
  // The strip is worth showing the moment there is ANY run signal — the brief,
  // a trace row, or a finished report. Before that the run has produced
  // nothing, and an empty strip would be chrome around a void.
  const hasRunSignal = Boolean(r.brief) || r.trace.length > 0 || Boolean(r.report);
  return (
    <>
      {/* Error state */}
      {r.status === "ERROR" && (
        <ErrorState
          message={r.error ?? "The research run failed."}
          onRetry={r.retry}
        />
      )}

      {/* Start-up loader — the gap between submit and the model's brief
          arriving (typically 3-8 s). Without this the area below the H1 is
          completely blank, which looks like a hang. */}
      {!hasRunSignal && r.status === "RUNNING" && (
        <div className="flex items-center gap-inline font-ui text-[0.86rem] text-text-muted">
          <Loader2 className="size-4 animate-spin text-accent" aria-hidden />
          Starting the research…
        </div>
      )}

      {/* During the run + after: the progress strip (collapses on finish) */}
      {hasRunSignal && (
        <DeepProgressStrip
          brief={r.brief}
          trace={r.trace}
          stats={r.stats}
          status={r.status}
          followUpStatus={r.followUpStatus}
        />
      )}

      {/* Mid-run steering. The transport was live from D3 but had no control
          until v2 — without this the user can watch the research but not
          redirect it. Live only while the run is, and only when a report has
          not landed (after that, questions are follow-ups). */}
      {r.status === "RUNNING" && !r.report && <DeepSteerInput onSteer={r.steer} />}

      {/* The bounded-by honest notice — when the engine bounded out */}
      {r.report?.bounded_by && (
        <DeepBoundedNotice
          report={r.report}
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
