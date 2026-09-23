import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { serializeReportToMarkdown } from "./deepResearch";
import type { ReportEvent } from "@/types/agent";

const report = JSON.parse(readFileSync(path.resolve(process.cwd(), "../packages/agent-server/tests/fixtures/report_qualifications.json"), "utf-8")) as ReportEvent;

describe("report qualification downloads", () => {
  it("keeps findings, citation numbers, and evidence meanings as literal prose", () => {
    const md = serializeReportToMarkdown(report);
    expect(md.indexOf("Evidence and review qualifications")).toBeLessThan(md.indexOf("Executive Summary"));
    expect(md).toContain("2 supported, 1 possible contradictions, 3 unresolved, 4 not checked");
    expect(md).toContain("it is not a contradiction");
    expect(md).toContain(String.raw`qualification \[1\]`);
    expect(md).toContain("Unverified: The other participant must fail");
    expect(md).toContain("Not researched: Long-term behavior");
    expect(md).not.toContain("[[p1]]");
    expect(md).not.toContain("<img src=");
    expect(md).toContain(String.raw`\*\*not markup\*\*`);
  });
  it.each(["unavailable", "incomplete", "verdict"])("retains %s review status", outcome => {
    const md = serializeReportToMarkdown({...report, meta: {review_outcome: outcome}});
    expect(md.includes("editorial review was unavailable")).toBe(outcome === "unavailable");
    expect(md.includes("Review found unresolved issues")).toBe(outcome === "incomplete");
    expect(md.includes("Evidence and review qualifications")).toBe(outcome !== "verdict");
  });
  it("handles legacy metadata without inventing measurements", () => {
    const md = serializeReportToMarkdown({...report, meta: {
      unverified_sentences: null, residual_deficiencies: ["Older finding", 12],
      grounding_counts: {supported: true, contradicted: 0, unresolved: 0, unavailable: 0},
      review_notes: "not a list", untested_angles: [false],
    }});
    expect(md).toContain("Unverified: Older finding");
    expect(md).not.toContain("Automated evidence check");
    expect(md).not.toContain("not a list");
    expect(md).not.toContain("Not researched:");
  });
});
