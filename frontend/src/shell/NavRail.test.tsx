import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { NavRail } from "./NavRail";
import { isFreshMode, resetSessionResumeStateForTests } from "@/lib/sessionResume";

// No running tasks → no badge noise in the rail.
vi.mock("@/hooks/useActivity", () => ({
  useRunningCount: () => 0,
}));

// Mock the mode context so old setMode calls fail the new "preserve mode" rule.
const setMode = vi.fn();
vi.mock("@/shell/mode", () => ({
  useMode: () => ({ mode: "build", setMode, modes: [] }),
}));

const renderRail = () =>
  render(
    <BrowserRouter>
      <NavRail collapsed={false} onToggleCollapse={vi.fn()} />
    </BrowserRouter>,
  );

describe("NavRail", () => {
  beforeEach(() => {
    setMode.mockClear();
    resetSessionResumeStateForTests();
  });

  it("marks a fresh session without changing the current mode when 'New' is clicked", () => {
    renderRail();
    fireEvent.click(screen.getByRole("link", { name: "New" }));
    expect(setMode).not.toHaveBeenCalled();
    expect(isFreshMode("build")).toBe(true);
    expect(isFreshMode("agent")).toBe(true);
  });

  it("does not touch the mode when a non-New item is clicked", () => {
    renderRail();
    fireEvent.click(screen.getByRole("link", { name: "History" }));
    expect(setMode).not.toHaveBeenCalled();
  });
});
