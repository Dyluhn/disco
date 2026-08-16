import { describe, expect, it } from "vitest";
import type { ReportEvent } from "@/types/agent";
import { asGroundedAnswer } from "./groundedAnswer";

function reportWithReviewedPassages(): ReportEvent {
  return {
    id: "report-1",
    seq: 4,
    ts: "2026-08-16T00:00:00Z",
    kind: "report",
    source: "agent",
    query: "What shipped?",
    summary: "A release shipped.",
    sections: [],
    passages: [
      {
        id: "cited",
        source_url: "https://example.com/cited",
        source_title: "Cited source",
        text: "The report cited this passage.",
      },
    ],
    reviewed_passages: [
      {
        id: "reviewed",
        source_url: "https://example.com/reviewed",
        source_title: "Reviewed source",
        text: "Muse Glimmer was released as open source.",
      },
      {
        id: "cited",
        source_url: "https://example.com/duplicate",
        source_title: "Duplicate copy",
        text: "A duplicate must not replace cited evidence.",
      },
    ],
    all_hits: [],
    unsupported_count: 0,
    bounded_by: null,
    depth_tier: "quick",
  };
}

describe("asGroundedAnswer", () => {
  it("makes cited and reviewed report passages resolvable, cited-first and deduplicated", () => {
    const answer = asGroundedAnswer(reportWithReviewedPassages(), "Which model shipped?");

    expect(answer?.passages.map((passage) => passage.id)).toEqual(["cited", "reviewed"]);
    expect(answer?.passages[0].source_title).toBe("Cited source");
  });
});
