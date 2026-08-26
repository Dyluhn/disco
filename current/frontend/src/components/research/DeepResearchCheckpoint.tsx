import { Pause } from "lucide-react";
import type { ResearchCheckpointEvent } from "@/types/agent";

interface Props {
  checkpoint: ResearchCheckpointEvent;
}

/** The paused state is evidence, not a report. Keep it visually distinct from
 * the product artifact and make the retained corpus visible without implying
 * that prose was written. */
export function DeepResearchCheckpoint({ checkpoint }: Props) {
  const retained = checkpoint.passages.length;
  return (
    <aside
      role="status"
      aria-label="Research paused"
      className="rounded-card border border-warn/40 bg-surface-1 px-body py-body"
      data-dr-checkpoint="true"
    >
      <div className="flex items-start gap-inline">
        <Pause className="mt-px size-4 shrink-0 text-warn" aria-hidden />
        <div>
          <div className="font-ui text-[0.84rem] font-medium text-text">
            Research paused before a report was written
          </div>
          <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
            {retained > 0
              ? `${retained} source${retained === 1 ? "" : "s"} retained. Resume to continue gathering and write the report.`
              : "No source evidence was retained. Resume to continue the research."}
          </p>
        </div>
      </div>
    </aside>
  );
}
