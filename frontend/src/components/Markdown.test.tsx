import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Markdown } from "./Markdown";
import type { GroundedAnswer } from "@/types/grounded";

// Mock Citation component since it has complex portal/dialog logic
vi.mock("./Citation", () => ({
  Citation: ({ n }: { n: number }) => <sup data-testid="citation-chip">[{n}]</sup>,
}));

describe("Markdown citation rendering", () => {
  const mockAnswer: GroundedAnswer = {
    query: "test",
    blocks: [],
    claims: [],
    passages: [
      { id: "s1", source_url: "url1", source_title: "Title 1", text: "Text 1" },
      { id: "s2", source_url: "url2", source_title: "Title 2", text: "Text 2" },
    ],
    all_hits: [],
    unsupported_count: 0,
    follow_ups: [],
  };

  it("renders raw [[id]] when no answer is provided", () => {
    render(<Markdown>This is [[s1]].</Markdown>);
    expect(screen.getByText("This is [[s1]].")).toBeInTheDocument();
    expect(screen.queryByTestId("citation-chip")).not.toBeInTheDocument();
  });

  it("renders citation chips in prose when answer is provided", () => {
    render(<Markdown answer={mockAnswer}>This is [[s1]].</Markdown>);
    expect(screen.getByText(/This is/)).toBeInTheDocument();
    expect(screen.getByTestId("citation-chip")).toHaveTextContent("[1]");
  });

  it("renders citation chips inside bold/italic text", () => {
    render(<Markdown answer={mockAnswer}>**Bold [[s1]]** and *italic [[s2]]*</Markdown>);
    expect(screen.getAllByTestId("citation-chip")).toHaveLength(2);
  });

  it("renders citation chips inside table cells", () => {
    render(
      <Markdown answer={mockAnswer}>
{"| H1 [[s1]] | H2 |\n|---|---|\n| R1 | R2 [[s2]] |"}
      </Markdown>
    );
    expect(screen.getByRole("table")).toBeInTheDocument();
    const chips = screen.getAllByTestId("citation-chip");
    expect(chips).toHaveLength(2);
    expect(chips[0]).toHaveTextContent("[1]");
    expect(chips[1]).toHaveTextContent("[2]");
  });

  it("renders placeholders for unknown citation IDs", () => {
    render(<Markdown answer={mockAnswer}>Unknown [[s3]]</Markdown>);
    expect(screen.getByText("·")).toBeInTheDocument();
  });
});
