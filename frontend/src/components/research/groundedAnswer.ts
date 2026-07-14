import type { ReportEvent } from "@/types/agent";
import type { GroundedAnswer, Passage, SearchHit } from "@/types/grounded";

export function asGroundedAnswer(
  report: ReportEvent | null,
  query: string,
): GroundedAnswer | null {
  if (!report) return null;
  return {
    query,
    blocks: [],
    claims: report.claims ?? [],
    passages: report.passages as unknown as Passage[],
    all_hits: report.all_hits as unknown as SearchHit[],
    unsupported_count: report.unsupported_count,
    follow_ups: [],
  };
}
