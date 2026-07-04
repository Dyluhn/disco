import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EmptyState, ErrorState } from "./states";

describe("states", () => {
  it("empty state reflects the philosophy", () => {
    render(<EmptyState />);
    expect(screen.getByRole("heading", { name: "Research" })).toBeInTheDocument();
    expect(screen.getByText(/sourced answers/i)).toBeInTheDocument();
  });

  it("error state shows the message and retries", async () => {
    const onRetry = vi.fn();
    render(<ErrorState message="retrieval timed out" onRetry={onRetry} />);
    expect(screen.getByText(/retrieval timed out/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
