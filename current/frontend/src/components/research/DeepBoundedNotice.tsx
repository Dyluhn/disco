/**
 * DeepBoundedNotice — the honest "what was cut" surface. When the engine's
 * ReportEvent sets `bounded_by`, a quiet aside renders outside the document
 * naming the research budget that closed the evidence loop. The report itself
 * remains a finished artifact — a bound is a budget that was reached, never an
 * incomplete checklist.
 *
 * Most tools hide this — a run "completes" and you're left guessing what's
 * been left out. Surfacing it elegantly is the trust move that fits this
 * product's discipline.
 */

import { Info, Layers, Zap } from "lucide-react";
import type { ReportEvent } from "@/types/agent";

interface Props {
  report: ReportEvent;
  onTryExhaustive?: () => void;
}

const BOUND_TEXT: Record<string, { what: string; lever: string }> = {
  sources: {
    what: "the web-source budget",
    lever:
      "Each tier caps unique web passages per execution. Further retrieval or refinement stopped when this run reached that cap.",
  },
  wall_clock: {
    what: "the wall-clock budget",
    lever: "The run hit its time budget before the model called the research done.",
  },
};

export function DeepBoundedNotice({ report, onTryExhaustive }: Props) {
  // "rounds" is the per-round DEPTH cap, not a coverage truncation, so it is
  // not surfaced (mirrors the backend report_truncation() shared by the
  // PDF/markdown/LLM-context exporters).
  if (!report.bounded_by || report.bounded_by === "rounds") return null;
  const text =
    BOUND_TEXT[report.bounded_by] ??
    { what: report.bounded_by, lever: "" };

  return (
    <aside
      role="note"
      aria-label="Research budget used"
      className="rounded-card border border-hairline bg-surface-1 px-body py-body"
    >
      <div className="flex items-start gap-inline">
        <Layers
          className="mt-px size-4 shrink-0 text-text-faint"
          aria-hidden
        />
        <div className="flex-1">
          <div className="font-ui text-[0.84rem] font-medium text-text">
            Report compiled from the strongest evidence gathered
          </div>
          <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
            This run was bounded by <span className="text-text">{text.what}</span>.{" "}
            {text.lever}
          </p>

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
                className="flex min-h-11 items-center gap-hair rounded-control border border-accent/40 px-inline py-hair font-ui text-[0.78rem] text-accent transition-colors hover:bg-accent hover:text-bg lg:min-h-0"
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
