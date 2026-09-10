import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EmptyState, ErrorState } from "./states";
import { ModeProvider } from "@/shell/ModeProvider";

describe("states", () => {
  it("empty state carries the brand + Latin entry, with NO duplicate mode chips", () => {
    render(
      <ModeProvider>
        <EmptyState />
      </ModeProvider>,
    );
    expect(screen.getByRole("heading", { name: "Disco" })).toBeInTheDocument();
    expect(screen.getByText(/I learn; I become acquainted with/i)).toBeInTheDocument();
    expect(screen.getByText(/sourced answers/i)).toBeInTheDocument();
    // The subchip row duplicated the top-bar mode switcher — removed.
    expect(screen.queryByRole("button", { name: "search" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "build" })).not.toBeInTheDocument();
    expect(document.querySelector('[data-disco-control="splash.surface-chips"]')).toBeNull();
  });

  it("error state shows the message and retries", async () => {
    const onRetry = vi.fn();
    render(<ErrorState message="retrieval timed out" onRetry={onRetry} />);
    expect(screen.getByText(/retrieval timed out/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
