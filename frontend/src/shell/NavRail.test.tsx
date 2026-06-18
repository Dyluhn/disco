import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { NavRail } from "./NavRail";

// No running tasks → no badge noise in the rail.
vi.mock("@/hooks/useActivity", () => ({
  useRunningCount: () => 0,
}));

// Mock the mode context so we can assert "New" resets the surface to Search.
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
  });

  it("resets the surface to the default search mode when 'New' is clicked", () => {
    renderRail();
    fireEvent.click(screen.getByRole("link", { name: "New" }));
    expect(setMode).toHaveBeenCalledWith("search");
  });

  it("does not touch the mode when a non-New item is clicked", () => {
    renderRail();
    fireEvent.click(screen.getByRole("link", { name: "History" }));
    expect(setMode).not.toHaveBeenCalled();
  });
});
