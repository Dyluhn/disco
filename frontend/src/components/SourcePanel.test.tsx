import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { rrfAnswer } from "@/fixtures/answers";
import { SourcePanel } from "./SourcePanel";

describe("SourcePanel", () => {
  it("renders the full discovery set with explicit failure states", () => {
    render(<SourcePanel answer={rrfAnswer} onReScope={vi.fn()} />);
    // failed sources are shown AS failed, never silently dropped (§13.3)
    expect(screen.getByText("Paywalled")).toBeInTheDocument();
    expect(screen.getByText("Blocked")).toBeInTheDocument();
    expect(screen.getByText("Not found")).toBeInTheDocument();
  });

  it("switches between All Searched and Cited", async () => {
    render(<SourcePanel answer={rrfAnswer} onReScope={vi.fn()} />);
    await userEvent.click(screen.getByRole("tab", { name: /Cited/ }));
    // a cited passage's source title appears only on the Cited tab
    expect(
      screen.getByText(/Reciprocal rank fusion \| Elasticsearch Guide/i),
    ).toBeInTheDocument();
  });

  it("re-scope controls re-issue retrieval", async () => {
    const onReScope = vi.fn();
    render(<SourcePanel answer={rrfAnswer} onReScope={onReScope} />);
    await userEvent.click(screen.getByRole("button", { name: /drop weak/i }));
    expect(onReScope).toHaveBeenCalledWith({ drop_weak: true });
    await userEvent.click(screen.getByRole("button", { name: /re-synthesize/i }));
    expect(onReScope).toHaveBeenCalledWith({});
  });
});
