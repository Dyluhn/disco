import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DeepReportView } from "./DeepReportView";
import type { ReportEvent, ReportSection } from "@/types/agent";

// Mock icons (same pattern as DeepReportView.table.test.tsx) — keeps the unit
// test focused on the presentation variance, not SVG internals.
vi.mock("lucide-react", () => ({
  AlertTriangle: () => <div data-testid="alert-triangle" />,
  CircleDot: () => <div data-testid="circle-dot" />,
  Loader2: () => <div data-testid="loader" />,
}));

function section(over: Partial<ReportSection> & { id: string }): ReportSection {
  return {
    title: over.title ?? over.id,
    markdown: "",
    cited_passage_ids: [],
    confidence: "high",
    disputed_notes: [],
    unsupported_count: 0,
    ...over,
  };
}

function report(sections: ReportSection[]): ReportEvent {
  return {
    type: "report",
    query: "q",
    summary: null,
    sections,
    passages: [],
    all_hits: [],
    unsupported_count: 0,
    depth_tier: "standard_deep",
    bounded_by: null,
  } as unknown as ReportEvent;
}

function renderReport(sections: ReportSection[]) {
  const rep = report(sections);
  return render(
    <DeepReportView
      query="q"
      summary={null}
      assembling={sections.map((s) => ({
        id: s.id,
        title: s.title,
        state: "done" as const,
        section: s,
      }))}
      report={rep}
    />,
  );
}

describe("DeepReportView — W-11 section presentation variance", () => {
  it("gives different section shapes different presentation (not one uniform template)", () => {
    const sections = [
      section({ id: "sec0", title: "Lead", markdown: "The headline finding [[p1]]." }),
      section({
        id: "sec1",
        title: "Comparison",
        markdown: "| Approach | Tradeoff |\n|---|---|\n| A | fast [[p1]] |",
      }),
      section({
        id: "sec2",
        title: "Options",
        markdown: "- first option [[p1]]\n- second option [[p1]]\n- third option [[p1]]",
      }),
      section({ id: "sec3", title: "Plain", markdown: "A plain analytical paragraph [[p1]]." }),
    ];
    const { container } = renderReport(sections);

    const lead = container.querySelector("#sec0")!;
    const table = container.querySelector("#sec1")!;
    const list = container.querySelector("#sec2")!;
    const plain = container.querySelector("#sec3")!;

    // First section gets the lead/standfirst treatment (accent rule).
    expect(lead.className).toContain("border-l-2");
    // A table-bearing section gets the data-card treatment.
    expect(table.className).toContain("rounded-card");
    // The shapes are genuinely distinct from one another — not all identical.
    const classNames = new Set([
      lead.className,
      table.className,
      list.className,
      plain.className,
    ]);
    expect(classNames.size).toBeGreaterThan(1);
    expect(lead.className).not.toEqual(table.className);

    // Eyebrow labels surface the role/shape honestly: a table-only section is
    // labeled "Table", NOT "Figure" (BW-06 — no false graph promise).
    expect(screen.getByText("Key finding")).toBeInTheDocument();
    expect(screen.getByText("Table")).toBeInTheDocument();
    expect(screen.queryByText("Figure")).not.toBeInTheDocument();
  });

  it("labels a table-only section 'Table' (not 'Figure') with the data-card treatment", () => {
    const sections = [
      section({ id: "lead", title: "Intro", markdown: "Opening [[p1]]." }),
      section({
        id: "data",
        title: "By the numbers",
        markdown: "Trend below.\n\n| Year | Share |\n|---|---|\n| 2024 | 40% [[p1]] |",
      }),
    ];
    const { container } = renderReport(sections);

    // BW-06: a table earns the honest "Table" eyebrow, never a false "Figure".
    expect(screen.getByText("Table")).toBeInTheDocument();
    expect(screen.queryByText("Figure")).not.toBeInTheDocument();
    expect(container.querySelector("#data")!.className).toContain("rounded-card");
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("stamps 'Figure' ONLY when a real ```chart``` block is present", () => {
    const sections = [
      section({ id: "lead", title: "Intro", markdown: "Opening [[p1]]." }),
      section({
        id: "graph",
        title: "Growth over time",
        markdown:
          "See the chart.\n\n```chart\n" +
          '{"chart_type":"line","title":"Growth","data":[{"label":"2024","value":1},{"label":"2025","value":2}]}' +
          "\n```",
      }),
    ];
    const { container } = renderReport(sections);

    // A genuine chart block keeps the "Figure" eyebrow (the graph promise is real).
    expect(screen.getByText("Figure")).toBeInTheDocument();
    expect(screen.queryByText("Table")).not.toBeInTheDocument();
    expect(container.querySelector("#graph")!.className).toContain("rounded-card");
  });

  it("does not crash and preserves content for a single (lead) section", () => {
    renderReport([section({ id: "only", title: "Only", markdown: "Body prose [[p1]]." })]);
    // Lead eyebrow shows; title + prose still render (grounding/content unchanged).
    expect(screen.getByText("Key finding")).toBeInTheDocument();
    expect(screen.getAllByText("Only").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/Body prose/)).toBeInTheDocument();
  });
});
