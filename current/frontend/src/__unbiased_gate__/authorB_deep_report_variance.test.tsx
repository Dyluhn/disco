import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DeepReportView } from "@/components/research/DeepReportView";
import type { ReportEvent, ReportSection } from "@/types/agent";

function section(
  id: string,
  title: string,
  markdown: string,
  cited: string[],
  confidence: ReportSection["confidence"] = "high",
): ReportSection {
  return {
    id,
    title,
    markdown,
    cited_passage_ids: cited,
    confidence,
    disputed_notes: [],
    unsupported_count: 0,
  };
}

const sections: ReportSection[] = [
  section("lead", "Where the market is moving", "Adoption is accelerating in pilots [[p1]].", [
    "p1",
  ]),
  section(
    "figure",
    "Funding trend",
    [
      "Funding has increased over time [[p2]].",
      "",
      "```chart",
      '{"chart_type":"line","title":"Funding","data":[{"label":"2024","value":1},{"label":"2025","value":2}]}',
      "```",
    ].join("\n"),
    ["p2"],
    "mixed",
  ),
  section(
    "list",
    "Near-term blockers",
    "- Manufacturing yield remains uneven [[p3]].\n- Qualification cycles remain slow [[p1]].",
    ["p3", "p1"],
  ),
  section("plain", "Regional outlook", "Europe is watching the same qualification data [[p2]].", [
    "p2",
  ]),
];

const report: ReportEvent = {
  id: "report-authorb-w11",
  kind: "report",
  query: "solid-state battery market",
  summary: "Executive summary stays grounded [[p1]].",
  sections,
  passages: [
    {
      id: "p1",
      source_url: "https://example.test/one",
      source_title: "Pilot source",
      text: "Pilot evidence",
    },
    {
      id: "p2",
      source_url: "https://example.test/two",
      source_title: "Funding source",
      text: "Funding evidence",
    },
    {
      id: "p3",
      source_url: "https://example.test/three",
      source_title: "Manufacturing source",
      text: "Manufacturing evidence",
    },
  ],
  all_hits: [],
  unsupported_count: 0,
  bounded_by: null,
  depth_tier: "standard_deep",
  claims: [
    {
      claim: { text: "Adoption is accelerating", cited_passage_ids: ["p1"] },
      verdict: "supported",
      best_passage_id: "p1",
      entailment_score: 0.93,
    },
    {
      claim: { text: "Funding has increased", cited_passage_ids: ["p2"] },
      verdict: "weak",
      best_passage_id: "p2",
      entailment_score: 0.58,
    },
  ],
};

describe("AuthorB unbiased gate — W-11 Deep Research report presentation", () => {
  it("W-11 renders varied section presentation while preserving citation grounding", () => {
    const assembling = report.sections.map((s) => ({
      id: s.id,
      title: s.title,
      state: "done" as const,
      section: s,
    }));

    const { container } = render(
      <DeepReportView
        query={report.query}
        summary={report.summary}
        assembling={assembling}
        report={report}
      />,
    );

    const renderedSections = sections.map((s) => container.querySelector(`section#${s.id}`));
    expect(renderedSections.every(Boolean)).toBe(true);
    expect(new Set(renderedSections.map((el) => el!.className)).size).toBeGreaterThan(1);
    expect(screen.getByText("Key finding")).toBeInTheDocument();
    expect(screen.getByText("Figure")).toBeInTheDocument();

    expect(container.textContent).not.toContain("[[p1]]");
    expect(container.textContent).not.toContain("[[p2]]");
    expect(container.textContent).not.toContain("[[p3]]");
    expect(screen.getAllByRole("button", { name: /^Source \d:/ })).toHaveLength(6);
    // UI-20: the meter names the checker's own states now. These fixture claims
    // predate `verification_status`, and a bare "weak" verdict is the neutral
    // measurement the checker calls unresolved.
    expect(screen.getByTitle("1 supported, 1 unresolved")).toBeInTheDocument();
  });
});
