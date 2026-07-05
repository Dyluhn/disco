import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CommandPalette } from "./CommandPalette";
import { BrowserRouter } from "react-router-dom";
import { isFreshMode, resetSessionResumeStateForTests } from "@/lib/sessionResume";

// Mock useTheme
vi.mock("@/lib/useTheme", () => ({
  useTheme: () => ({
    theme: "dark",
    toggle: vi.fn(),
  }),
}));

// Mock the old mode context call so the new "preserve mode" behavior can assert
// that New no longer forces Search.
const setMode = vi.fn();
vi.mock("@/shell/mode", () => ({
  useMode: () => ({ mode: "build", setMode, modes: [] }),
}));

const renderPalette = () => {
  return render(
    <BrowserRouter>
      <CommandPalette />
    </BrowserRouter>
  );
};

describe("CommandPalette", () => {
  beforeEach(() => {
    setMode.mockClear();
    resetSessionResumeStateForTests();
  });
  it("should open on Ctrl+K", () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    expect(screen.getByPlaceholderText("Search commands...")).toBeDefined();
  });

  it("should filter commands", () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByPlaceholderText("Search commands...");
    
    fireEvent.change(input, { target: { value: "history" } });
    
    expect(screen.getByText("History")).toBeDefined();
    expect(screen.queryByText("Projects")).toBeNull();
  });

  it("should close on Escape", () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    expect(screen.getByPlaceholderText("Search commands...")).toBeDefined();
    
    fireEvent.keyDown(screen.getByPlaceholderText("Search commands..."), { key: "Escape" });
    // Radix Dialog handles Escape, but we can check if it's gone from the DOM
    // depending on how Portal works in the test environment.
    // In many test setups, we might need to wait or check the open state.
    expect(screen.queryByPlaceholderText("Search commands...")).toBeNull();
  });

  // Regression for gap #91: arrow-key handling used `% filteredCommands.length`,
  // which is `NaN` when the query matches nothing. The guard must make Arrow keys a
  // no-op on an empty result set (and keep the empty-state message rendered).
  it("does not crash on arrow keys when no commands match (empty-results modulo guard)", () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    const input = screen.getByPlaceholderText("Search commands...");
    fireEvent.change(input, { target: { value: "zzzznomatch" } });
    expect(screen.getByText("No commands found.")).toBeDefined();
    // These would throw / wedge selection if the modulo weren't guarded.
    expect(() => {
      fireEvent.keyDown(input, { key: "ArrowDown" });
      fireEvent.keyDown(input, { key: "ArrowUp" });
      fireEvent.keyDown(input, { key: "Enter" });
    }).not.toThrow();
    // Still showing the empty state — nothing got selected or actioned.
    expect(screen.getByText("No commands found.")).toBeDefined();
  });

  it("marks a fresh session without resetting the active mode when 'New' is invoked", () => {
    renderPalette();
    fireEvent.keyDown(document, { key: "k", ctrlKey: true });
    fireEvent.click(screen.getByText("New"));
    expect(setMode).not.toHaveBeenCalled();
    expect(isFreshMode("build")).toBe(true);
    expect(isFreshMode("agent")).toBe(true);
  });
});
