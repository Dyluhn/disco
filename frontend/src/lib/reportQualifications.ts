/** Qualifications retained by every report download; mirror _report_qualifications.py. */
import type { ReportEvent } from "@/types/agent";
import { UNRESOLVED_MEANING } from "./claimCheck";

export function reportQualifications(report: ReportEvent): string[] {
  const meta = report.meta ?? {};
  const notes: string[] = [];
  if (meta.review_outcome === "unavailable") {
    notes.push("The model's editorial review was unavailable for this run.");
  } else if (meta.review_outcome === "incomplete") {
    notes.push("Review found unresolved issues in this report. See the findings below.");
  }
  const raw = meta.grounding_counts;
  const keys = ["supported", "contradicted", "unresolved", "unavailable"];
  if (typeof raw === "object" && raw !== null) {
    const counts = raw as Record<string, unknown>;
    if (keys.every(key => typeof counts[key] === "number" && Number.isInteger(counts[key]) && Number(counts[key]) >= 0)) {
      notes.push(`Automated evidence check: ${counts.supported} supported, ${counts.contradicted} possible contradictions, ${counts.unresolved} unresolved, ${counts.unavailable} not checked.`);
      notes.push(UNRESOLVED_MEANING);
    }
  }
  const groups: Array<[unknown, string]> = [
    [meta.review_notes, ""],
    [meta.unverified_sentences ?? meta.residual_deficiencies, "Unverified: "],
    [meta.untested_angles, "Not researched: "],
  ];
  for (const [values, prefix] of groups) {
    if (Array.isArray(values)) {
      notes.push(...values.filter((item): item is string => typeof item === "string" && item.trim() !== "").map(item => prefix + item));
    }
  }
  return notes;
}
