import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BlockView } from "./blocks";
import type { AnswerBlock } from "@/types/grounded";

// SheetBlock is an HONEST PREVIEW CARD — no in-block download. The .xlsx is a
// real workspace artifact delivered via DeliverableEvent (which holds the cid);
// the block render path (BlockView) has no cid, so a self-fetching button could
// never resolve the file and would be a false affordance. These tests assert the
// preview metadata renders AND that no download button is present.

describe("SheetBlock component", () => {
  it("renders a sheet card with title and filename", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s1",
      title: "Q4 Sales Report",
      filename: "q4_sales.xlsx",
      sheet_names: ["Revenue", "Costs", "Summary"],
      formulas_evaluated: false,
    };
    render(<BlockView block={block} answer={null} />);

    expect(screen.getByText("Q4 Sales Report")).toBeInTheDocument();
    // filename appears twice: header (mono) + disclaimer provenance line.
    expect(screen.getAllByText("q4_sales.xlsx").length).toBeGreaterThanOrEqual(1);
  });

  it("renders all sheet names as badges", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s2",
      title: "Budget",
      filename: "budget.xlsx",
      sheet_names: ["Income", "Expenses"],
      formulas_evaluated: false,
    };
    render(<BlockView block={block} answer={null} />);

    expect(screen.getByText("Income")).toBeInTheDocument();
    expect(screen.getByText("Expenses")).toBeInTheDocument();
    expect(screen.getByText("2 sheets")).toBeInTheDocument();
  });

  it("renders singular '1 sheet' for single-sheet workbook", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s3",
      title: "Data",
      filename: "data.xlsx",
      sheet_names: ["Sheet1"],
      formulas_evaluated: false,
    };
    render(<BlockView block={block} answer={null} />);

    expect(screen.getByText("1 sheet")).toBeInTheDocument();
  });

  it("shows the formula evaluation disclaimer", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s4",
      title: "Report",
      filename: "report.xlsx",
      sheet_names: ["Data"],
      formulas_evaluated: false,
    };
    render(<BlockView block={block} answer={null} />);

    expect(screen.getByText(/formulas are/i)).toBeInTheDocument();
    expect(screen.getByText(/not evaluated/i)).toBeInTheDocument();
  });

  it("states the workbook is saved to the workspace (provenance, not a download)", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s5",
      title: "Saved Test",
      filename: "saved.xlsx",
      sheet_names: ["Data"],
      formulas_evaluated: false,
    };
    render(<BlockView block={block} answer={null} />);

    expect(screen.getByText(/saved to the workspace/i)).toBeInTheDocument();
    // filename appears twice: header (mono) + disclaimer — at least one present.
    expect(screen.getAllByText("saved.xlsx").length).toBeGreaterThanOrEqual(1);
  });

  it("renders NO download button (no false affordance — there is no cid here)", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s6",
      title: "No Button Test",
      filename: "nobutton.xlsx",
      sheet_names: ["Data"],
      formulas_evaluated: false,
    };
    const { container } = render(<BlockView block={block} answer={null} />);

    // A self-fetching download button on this render path would always 404.
    // Lock the honest-preview fix in place: assert there is no button at all.
    expect(container.querySelector("button")).toBeNull();
    expect(screen.queryByRole("button", { name: /download/i })).toBeNull();
  });

  it("renders the FileSpreadsheet icon", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s7",
      title: "Icon Test",
      filename: "icon.xlsx",
      sheet_names: ["Sheet1"],
      formulas_evaluated: false,
    };
    const { container } = render(<BlockView block={block} answer={null} />);

    // The lucide FileSpreadsheet icon renders an SVG
    const svg = container.querySelector("svg");
    expect(svg).toBeInTheDocument();
  });
});
