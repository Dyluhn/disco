/**
 * DeepBoundedNotice — the honest "what was cut" surface. When the engine's
 * ReportEvent sets `bounded_by`, a quiet aside renders above the report
 * naming what stopped the run, which sub-questions weren't covered, and the
 * lever to go deeper. Not alarmist; just true.
 *
 * Most tools hide this — a run "completes" and you're left guessing what's
 * been left out. Surfacing it elegantly is the trust move that fits this
 * product's discipline.
 */

import { Info, Layers, Zap } from "lucide-react";
import type { DeepPlanView } from "@/lib/deepResearchTrace";
import type { ReportEvent } from "@/types/agent";

interface Props {
  report: ReportEvent;
  plan: DeepPlanView | null;
  onTryExhaustive?: () => void;
}

const BOUND_TEXT: Record<string, { what: string; lever: string }> = {
  sources: {
    what: "the whole-run source budget",
    lever:
      "Each tier caps the total passages accumulated. This run hit the source budget before covering the rest of the plan.",
  },
  wall_clock: {
    what: "the wall-clock budget",
    lever: "The run hit its time budget before completing the plan.",
  },
  subquestions: {
    what: "the sub-question width cap",
    lever:
      "The plan proposed more sub-questions than this tier covers; the rest were trimmed before the gather phase.",
  },
};

export function DeepBoundedNotice({ report, plan, onTryExhaustive }: Props) {
  // "rounds" is the per-sub-question DEPTH cap, not a coverage truncation — every
  // sub-question still produces a section — so it is not surfaced (mirrors the backend
  // report_truncation() shared by the PDF/markdown/LLM-context exporters).
  if (!report.bounded_by || report.bounded_by === "rounds") return null;
  const text =
    BOUND_TEXT[report.bounded_by] ??
    { what: report.bounded_by, lever: "" };

  // figure out which sub-questions weren't covered (best-effort)
  const coveredTitles = new Set(report.sections.map((s) => s.title));
  const uncovered =
    plan?.steps.filter((s) => !coveredTitles.has(s.title)).map((s) => s.title) ??
    [];

  return (
    <aside
      role="note"
      aria-label="What this run did and didn't cover"
      className="rounded-card border border-hairline bg-surface-1 px-body py-body"
    >
      <div className="flex items-start gap-inline">
        <Layers
          className="mt-px size-4 shrink-0 text-text-faint"
          aria-hidden
        />
        <div className="flex-1">
          <div className="font-ui text-[0.84rem] font-medium text-text">
            Reached {report.sections.length} of {plan?.steps.length ?? report.sections.length} planned sub-questions
          </div>
          <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
            This run was bounded by <span className="text-text">{text.what}</span>.{" "}
            {text.lever}
          </p>

          {uncovered.length > 0 && (
            <div className="mt-inline font-ui text-[0.82rem] text-text-muted">
              <span className="text-text-faint">Not covered: </span>
              {uncovered.map((title, i) => (
                <span key={title}>
                  {i > 0 && <span className="text-text-faint">; </span>}
                  <span className="text-text">{title}</span>
                </span>
              ))}
            </div>
          )}

          <div className="mt-body flex flex-wrap items-center gap-inline">
            {report.depth_tier && (
              <span className="flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.72rem] uppercase tracking-wide text-text-faint">
                <Info className="size-2.5" aria-hidden />
                Tier: {report.depth_tier.replace("_", " ")}
              </span>
            )}
            {onTryExhaustive && report.depth_tier !== "exhaustive" && (
              <button
                type="button"
                onClick={onTryExhaustive}
                data-disco-control="dr.retry-exhaustive"
                className="flex items-center gap-hair rounded-control border border-accent/40 px-inline py-hair font-ui text-[0.78rem] text-accent transition-colors hover:bg-accent hover:text-bg"
              >
                <Zap className="size-3" aria-hidden />
                Run on Exhaustive tier
              </button>
            )}
          </div>
        </div>
      </div>
    </aside>
  );
}
