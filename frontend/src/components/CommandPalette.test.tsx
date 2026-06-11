import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CommandPalette } from "./CommandPalette";
import { BrowserRouter } from "react-router-dom";

// Mock useTheme
vi.mock("@/lib/useTheme", () => ({
  useTheme: () => ({
    theme: "dark",
    toggle: vi.fn(),
  }),
}));

const renderPalette = () => {
  return render(
    <BrowserRouter>
      <CommandPalette />
    </BrowserRouter>
  );
};

describe("CommandPalette", () => {
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
});
