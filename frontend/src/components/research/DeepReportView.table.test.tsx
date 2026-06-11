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
