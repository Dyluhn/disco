import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BlockView } from "./blocks";
import type { AnswerBlock } from "@/types/grounded";

// SheetBlock is an HONEST PREVIEW CARD by default. The .xlsx is a real workspace
// artifact delivered via DeliverableEvent (which holds the cid); without a cid
// threaded down, a self-fetching button would always 404 (a false affordance).
//
// D12: when a cid IS threaded in, the in-block download appears and points at
// the declared-artifact route — reusing ActivityFeed's SheetDownload (not a
// fork). When NO cid is present, the button stays absent. These tests assert
// both halves of that contract.

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

  it("renders NO download button when cid is absent (no false affordance)", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s6",
      title: "No Button Test",
      filename: "nobutton.xlsx",
      sheet_names: ["Data"],
      formulas_evaluated: false,
    };
    const { container } = render(<BlockView block={block} answer={null} />);

    // A self-fetching download button without a cid would always 404 — never
    // render one. The honest download lives in the Build/Agent ActivityFeed
    // (which has a cid).
    expect(container.querySelector("a[download]")).toBeNull();
    expect(screen.queryByRole("link", { name: /download/i })).toBeNull();
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

  // ---- D12: cid-threading contract -----------------------------------------

  it("renders NO download when cid is explicitly null (no false affordance)", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s8",
      title: "Explicit Null",
      filename: "explicit.xlsx",
      sheet_names: ["Data"],
      formulas_evaluated: false,
    };
    const { container } = render(<BlockView block={block} answer={null} cid={null} />);
    expect(container.querySelector("a[download]")).toBeNull();
  });

  it("renders a working in-block download when a cid is threaded in (D12)", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s9",
      title: "Q4 Sales Report",
      filename: "q4_sales.xlsx",
      sheet_names: ["Revenue", "Costs", "Summary"],
      formulas_evaluated: false,
    };
    const CID = "conv_sheet_block_1";
    render(<BlockView block={block} answer={null} cid={CID} />);

    // The download link reuses ActivityFeed's SheetDownload verbatim (same
    // <a download>, same declared-artifact route). It must resolve against
    // /conversations/{cid}/artifacts/{filename}.
    const link = screen.getByRole("link", { name: /Q4 Sales Report/i });
    expect(link).toHaveAttribute("download");
    expect(link.getAttribute("href")).toContain(`/conversations/${CID}/artifacts/q4_sales.xlsx`);
  });

  it("preserves subdir slashes in the filename (encodeURI, not encodeURIComponent)", () => {
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s10",
      title: "Subdir Report",
      filename: "reports/q4_sales.xlsx",
      sheet_names: ["Revenue"],
      formulas_evaluated: false,
    };
    const CID = "conv_subdir_1";
    render(<BlockView block={block} answer={null} cid={CID} />);

    const link = screen.getByRole("link", { name: /Subdir Report/i });
    expect(link.getAttribute("href")).toContain(
      `/conversations/${CID}/artifacts/reports/q4_sales.xlsx`,
    );
  });

  it("in-block download and ActivityFeed download use the same href shape", () => {
    // Lock the D12 contract: SheetBlock reuses SheetDownload, not a fork. The
    // href must match the ActivityFeed's expected URL byte-for-byte.
    const block: AnswerBlock = {
      kind: "sheet",
      id: "s11",
      title: "Shared Logic",
      filename: "shared.xlsx",
      sheet_names: ["One", "Two"],
      formulas_evaluated: false,
    };
    const CID = "conv_shared_1";
    render(<BlockView block={block} answer={null} cid={CID} />);

    const link = screen.getByRole("link", { name: /Shared Logic/i });
    // The ActivityFeed test asserts the same shape:
    //   /conversations/{CID}/artifacts/{filename}
    // If those ever diverge, the sheet-download UX breaks across surfaces.
    expect(link.getAttribute("href")).toMatch(
      new RegExp(`/conversations/${CID}/artifacts/shared\\.xlsx$`),
    );
    expect(link).toHaveAttribute("download");
  });
});
