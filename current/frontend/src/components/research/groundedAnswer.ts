import type { ReportEvent } from "@/types/agent";
import type { GroundedAnswer, Passage, SearchHit } from "@/types/grounded";

export function asGroundedAnswer(
  report: ReportEvent | null,
  query: string,
): GroundedAnswer | null {
  if (!report) return null;
  const seen = new Set<string>();
  const passages = [
    ...report.passages,
    ...(report.reviewed_passages ?? []),
  ].filter((passage) => {
    const id = typeof passage.id === "string" ? passage.id : "";
    if (!id || !seen.has(id)) {
      if (id) seen.add(id);
      return true;
    }
    return false;
  });
  return {
    query,
    blocks: [],
    claims: report.claims ?? [],
    passages: passages as unknown as Passage[],
    all_hits: report.all_hits as unknown as SearchHit[],
    unsupported_count: report.unsupported_count,
    follow_ups: [],
  };
}
