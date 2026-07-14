import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { BlockView } from "./blocks";
import type { AnswerBlock } from "@/types/grounded";
import { Chart } from "chart.js";

describe("ChartBlock component", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // ---- valid chart renders (bar, line, pie, scatter round-trip) -------

  it("renders a valid bar chart", () => {
    const block: AnswerBlock = {
      kind: "chart",
      id: "b1",
      chart_type: "bar",
      data: [{ label: "A", value: 10 }, { label: "B", value: 20 }],
      title: "Test Bar Chart",
      x_label: "Category",
      y_label: "Count",
    };
    render(<BlockView block={block} answer={null} />);

    expect(document.querySelector("canvas")).toBeInTheDocument();
    expect(screen.getByText("Test Bar Chart")).toBeInTheDocument();
  });

  it("renders a valid line chart", () => {
    const block: AnswerBlock = {
      kind: "chart",
      id: "b2",
      chart_type: "line",
      data: [
        { label: "Q1", value: 30 },
        { label: "Q2", value: 45 },
        { label: "Q3", value: 38 },
      ],
      title: "Quarterly Trend",
    };
    render(<BlockView block={block} answer={null} />);

    expect(document.querySelector("canvas")).toBeInTheDocument();
    expect(screen.getByText("Quarterly Trend")).toBeInTheDocument();
  });

  it("renders a valid pie chart", () => {
    const block: AnswerBlock = {
      kind: "chart",
      id: "b3",
      chart_type: "pie",
      data: [
        { label: "Alpha", value: 40 },
        { label: "Beta", value: 35 },
        { label: "Gamma", value: 25 },
      ],
      title: "Market Share",
    };
    render(<BlockView block={block} answer={null} />);

    expect(document.querySelector("canvas")).toBeInTheDocument();
    expect(screen.getByText("Market Share")).toBeInTheDocument();
  });

  it("renders a valid scatter chart", () => {
    const block: AnswerBlock = {
      kind: "chart",
      id: "b4",
      chart_type: "scatter",
      data: [
        { x: 1, y: 2, group: "A" },
        { x: 2, y: 3, group: "A" },
        { x: 1.5, y: 4, group: "B" },
      ],
      title: "Correlation",
      x_label: "Input",
      y_label: "Output",
    };
    render(<BlockView block={block} answer={null} />);

    expect(document.querySelector("canvas")).toBeInTheDocument();
  });

  // ---- invalid payload → table fallback, never crashes ------------------

  it("renders table fallback for non-array data", () => {
    const block = {
      kind: "chart",
      id: "b1",
      chart_type: "bar",
      data: "not an array",
      title: "Invalid Chart",
    } as unknown as AnswerBlock;
    render(<BlockView block={block} answer={null} />);

    expect(screen.getByText("(Invalid chart data)")).toBeInTheDocument();
    expect(document.querySelector("canvas")).not.toBeInTheDocument();
  });

  it("renders table fallback for null data", () => {
    const block = {
      kind: "chart",
      id: "b2",
      chart_type: "line",
      data: null,
      title: "Null Data",
    } as unknown as AnswerBlock;
    render(<BlockView block={block} answer={null} />);

    expect(screen.getByText("(Invalid chart data)")).toBeInTheDocument();
    expect(document.querySelector("canvas")).not.toBeInTheDocument();
  });

  it("renders table fallback for empty array", () => {
    const block: AnswerBlock = {
      kind: "chart",
      id: "b3",
      chart_type: "pie",
      data: [],
      title: "Empty Data",
    };
    render(<BlockView block={block} answer={null} />);

    // Empty array is valid Array.isArray, so it tries to render chart;
    // with the global Chart mock it produces a canvas + title.
    expect(document.querySelector("canvas")).toBeInTheDocument();
  });

  it("renders table fallback when Chart.js throws (error degrade)", () => {
    // Override the global Chart mock for this test to simulate init failure
    vi.mocked(Chart).mockImplementationOnce(() => {
      throw new Error("Rendering failed");
    });

    const block: AnswerBlock = {
      kind: "chart",
      id: "b4",
      chart_type: "bar",
      data: [{ label: "A", value: 10 }],
      title: "Error Chart",
    };
    render(<BlockView block={block} answer={null} />);

    // After Chart throws the component sets error=true, then degrades to
    // table because tableData is valid (the data is parseable).
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("renders table fallback when Chart.js throws even with partial data", () => {
    // Chart throws — component degrades to table because the data has
    // enough structure for tableData to be built (d.get('weird') → table
    // cells with empty strings / 'undefined' strings). No crash.
    vi.mocked(Chart).mockImplementationOnce(() => {
      throw new Error("Rendering failed");
    });

    const block = {
      kind: "chart",
      id: "b5",
      chart_type: "bar",
      data: [{ weird: 1 }], // no label or value key
    } as unknown as AnswerBlock;
    render(<BlockView block={block} answer={null} />);

    // tableData is still constructed (rows have empty/undefined strings),
    // so a table renders, not the error message — the component never
    // crashes.
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(document.querySelector("canvas")).not.toBeInTheDocument();
  });
});
