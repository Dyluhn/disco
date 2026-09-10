import { Pause } from "lucide-react";
import type { ResearchCheckpointEvent } from "@/types/agent";

interface Props {
  checkpoint: ResearchCheckpointEvent;
}

/** The paused state is evidence, not a report. Keep it visually distinct from
 * the product artifact and make the retained corpus visible without implying
 * that prose was written. */
export function DeepResearchCheckpoint({ checkpoint }: Props) {
  const recovered = checkpoint.meta?.recovery_reason === "server_restart";
  const writing = checkpoint.meta?.recovery_stage === "writing";
  const savedCount = checkpoint.meta?.recovery_source_count;
  const retained = typeof savedCount === "number" && Number.isInteger(savedCount) && savedCount >= 0
    ? savedCount : checkpoint.passages.length;
  const turnsLeft = checkpoint.meta?.turns_remaining;
  const sourcesLeft = checkpoint.meta?.sources_remaining;
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
            {recovered ? "Research recovered after a server restart" : "Research paused before a report was written"}
          </div>
          <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
            {retained > 0
              ? `${retained} source${retained === 1 ? "" : "s"} retained. ${writing
                ? "Resume to restart writing from the saved evidence."
                : "Resume to continue from the saved evidence and write the report."}`
              : "No source evidence was retained. Resume to continue the research."}
          </p>
          {recovered && (
            <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
              {writing ? "Completed research will not be repeated." : "The interrupted request may be repeated."}
              {!writing && typeof turnsLeft === "number" && typeof sourcesLeft === "number"
                ? ` Remaining budget: ${turnsLeft} research turns and ${sourcesLeft} new sources.` : ""}
              {checkpoint.meta?.recovery_fallback === true
                ? " The latest saved state was unreadable; an earlier saved state was recovered." : ""}
            </p>
          )}
        </div>
      </div>
    </aside>
  );
}
