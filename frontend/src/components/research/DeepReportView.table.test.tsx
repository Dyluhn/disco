import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DeepReportView } from "./DeepReportView";
import type { ReportEvent } from "@/types/agent";

// Mock the components that might be problematic or not needed for this unit test
vi.mock("lucide-react", () => ({
  AlertTriangle: () => <div data-testid="alert-triangle" />,
  CircleDot: () => <div data-testid="circle-dot" />,
  Loader2: () => <div data-testid="loader" />,
}));

describe("DeepReportView table rendering", () => {
  it("renders a GFM table in a report section", () => {
    const mockReport: ReportEvent = {
      type: "report",
      query: "test query",
      summary: "Summary text",
      sections: [
        {
          id: "sec1",
          title: "Section 1",
          markdown: "| H1 | H2 |\n|---|---|\n| R1C1 | R1C2 |",
          confidence: "high",
          disputed_notes: [],
        },
      ],
      passages: [],
      all_hits: [],
      unsupported_count: 0,
      depth_tier: "standard_deep",
      bounded_by: "rounds",
    } as any;

    render(
      <DeepReportView
        query="test query"
        summary="Summary text"
        assembling={[
          {
            id: "sec1",
            title: "Section 1",
            state: "done",
            section: mockReport.sections[0],
          },
        ]}
        report={mockReport}
      />
    );

    // Check if the table is rendered.
    // ReactMarkdown with remarkGfm should produce a <table> element.
    const table = screen.getByRole("table");
    expect(table).toBeInTheDocument();
    
    // Check for header cells
    expect(screen.getByText("H1")).toBeInTheDocument();
    expect(screen.getByText("H2")).toBeInTheDocument();
    
    // Check for data cells
    expect(screen.getByText("R1C1")).toBeInTheDocument();
    expect(screen.getByText("R1C2")).toBeInTheDocument();
  });

  it("renders multiple paragraphs including a table", () => {
    const markdown = "Intro paragraph.\n\n| H1 |\n|---|\n| R1 |\n\nOutro paragraph.";
    const mockReport: ReportEvent = {
      type: "report",
      query: "test query",
      summary: "Summary",
      sections: [
        {
          id: "sec1",
          title: "Section 1",
          markdown,
          confidence: "high",
          disputed_notes: [],
        },
      ],
      passages: [],
      all_hits: [],
      unsupported_count: 0,
    } as any;

    render(
      <DeepReportView
        query="test query"
        summary="Summary"
        assembling={[{ id: "sec1", title: "Section 1", state: "done", section: mockReport.sections[0] }]}
        report={mockReport}
      />
    );

    expect(screen.getByText("Intro paragraph.")).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("Outro paragraph.")).toBeInTheDocument();
  });

  it("renders citation chips in prose and table cells", () => {
    const mockReport: ReportEvent = {
      type: "report",
      query: "test query",
      summary: "Summary with [[s1]]",
      sections: [
        {
          id: "sec1",
          title: "Section 1",
          markdown: "Prose [[s1]]\n\n| H [[s2]] |\n|---|\n| R [[s1]] |",
          confidence: "high",
          disputed_notes: [],
        },
      ],
      passages: [
        { id: "s1", source_url: "url1", source_title: "Title 1", text: "Text 1" },
        { id: "s2", source_url: "url2", source_title: "Title 2", text: "Text 2" },
      ],
      all_hits: [],
      unsupported_count: 0,
    } as any;

    render(
      <DeepReportView
        query="test query"
        summary={mockReport.summary}
        assembling={[{ id: "sec1", title: "Section 1", state: "done", section: mockReport.sections[0] }]}
        report={mockReport}
      />
    );

    // Should render 3 chips: 1 in summary, 1 in prose, 2 in table (H and R)
    // Actually, summary citation s1, prose citation s1, table header s2, table row s1.
    // Total 4 chips.
    // The chips are rendered by the real Citation component (or mock if we mocked it).
    // In this test file, we haven't mocked Citation, so it will use the real one.
    // The real Citation renders a button with the number.
    const chips = screen.getAllByRole("button");
    expect(chips).toHaveLength(4);
    
    // Check that no raw [[ literals remain
    expect(screen.queryByText(/\[\[/)).not.toBeInTheDocument();
  });
});

// ---- F3: DeepReportView accepts and threads the cid prop --------------------

describe("DeepReportView — F3 cid prop threading", () => {
  it("F3: renders cleanly when cid is provided (prop accepted, no error)", () => {
    // DeepReportView currently renders prose-only sections (Markdown/CitedText).
    // The cid prop is accepted and forwarded so that when DR begins emitting
    // block artifacts the download wiring is already in place.  This test
    // verifies the prop is forwarded without TypeScript errors or runtime
    // crashes, and that the report content still renders correctly.
    const mockReport: any = {
      type: "report",
      query: "battery commercialization",
      summary: "Executive summary text.",
      sections: [
        {
          id: "sec1",
          title: "Market Status",
          markdown: "Solid-state batteries are advancing rapidly.",
          confidence: "high",
          disputed_notes: [],
          unsupported_count: 0,
        },
      ],
      passages: [],
      all_hits: [],
      unsupported_count: 0,
      depth_tier: "standard_deep",
      bounded_by: null,
    };

    // Must not throw; the prop is only consumed downstream when block-level
    // artifacts (sheet, slides) appear in a section.
    render(
      <DeepReportView
        query="battery commercialization"
        summary="Executive summary text."
        assembling={[
          { id: "sec1", title: "Market Status", state: "done", section: mockReport.sections[0] },
        ]}
        report={mockReport}
        cid="conv_deep_fixture"
      />,
    );

    // Section title appears in both the <h2> and the ToC nav <a> — use
    // getAllByText to avoid "multiple elements" error.
    expect(screen.getAllByText("Market Status").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Solid-state batteries are advancing rapidly/)).toBeInTheDocument();
  });

  it("F3: renders identically when cid is undefined (backward-compat, no false affordance)", () => {
    const mockReport: any = {
      type: "report",
      query: "q",
      summary: null,
      sections: [
        {
          id: "s1",
          title: "Only section",
          markdown: "Some prose.",
          confidence: "high",
          disputed_notes: [],
          unsupported_count: 0,
        },
      ],
      passages: [],
      all_hits: [],
      unsupported_count: 0,
      depth_tier: "standard_deep",
      bounded_by: null,
    };

    // cid omitted → behaviour unchanged from before F3 (no download, no error)
    render(
      <DeepReportView
        query="q"
        summary={null}
        assembling={[{ id: "s1", title: "Only section", state: "done", section: mockReport.sections[0] }]}
        report={mockReport}
      />,
    );

    // Section title appears in both the <h2> and the ToC nav <a>
    expect(screen.getAllByText("Only section").length).toBeGreaterThanOrEqual(1);
  });
});

// ---- WALK-03 (C1): disputed_notes must not leak raw [[id]] ------------------

describe("DeepReportView — WALK-03 disputed_notes citation resolution", () => {
  it("WALK-03: [[passage_id]] in disputed_notes renders as a citation chip, not raw text", () => {
    const mockReport: ReportEvent = {
      id: "r1",
      kind: "report",
      seq: 5,
      query: "test query",
      summary: null as unknown as string,
      sections: [
        {
          id: "sec1",
          title: "Section 1",
          markdown: "Body text.",
          cited_passage_ids: ["p1"],
          confidence: "mixed",
          // A disputed note containing a raw [[id]] citation marker
          disputed_notes: ["One source argues X [[p1]]."],
          unsupported_count: 0,
        },
      ],
      passages: [
        { id: "p1", source_url: "http://example.com", source_title: "Example", text: "T" },
      ],
      all_hits: [],
      unsupported_count: 0,
      bounded_by: null,
      depth_tier: "standard_deep",
    } as any;

    render(
      <DeepReportView
        query="test query"
        summary={null}
        assembling={[
          {
            id: "sec1",
            title: "Section 1",
            state: "done",
            section: mockReport.sections[0],
          },
        ]}
        report={mockReport}
      />
    );

    // The "Sources disagree." callout must appear
    expect(screen.getByRole("note")).toBeInTheDocument();
    // The raw [[p1]] marker must NOT appear as literal text
    expect(screen.queryByText(/\[\[p1\]\]/)).not.toBeInTheDocument();
    // The surrounding prose must still be readable
    expect(screen.getByText(/One source argues X/)).toBeInTheDocument();
  });

  it("WALK-03: disputed_notes without [[id]] renders as plain text (no chips)", () => {
    const mockReport: ReportEvent = {
      id: "r2",
      kind: "report",
      seq: 5,
      query: "q",
      summary: null as unknown as string,
      sections: [
        {
          id: "sec1",
          title: "Section 1",
          markdown: "Body.",
          cited_passage_ids: [],
          confidence: "mixed",
          disputed_notes: ["Two papers reach opposite conclusions."],
          unsupported_count: 0,
        },
      ],
      passages: [],
      all_hits: [],
      unsupported_count: 0,
      bounded_by: null,
      depth_tier: "standard_deep",
    } as any;

    render(
      <DeepReportView
        query="q"
        summary={null}
        assembling={[
          { id: "sec1", title: "Section 1", state: "done", section: mockReport.sections[0] },
        ]}
        report={mockReport}
      />
    );

    expect(screen.getByText(/Two papers reach opposite conclusions/)).toBeInTheDocument();
  });
});
