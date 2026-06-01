import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ModeProvider } from "./ModeProvider";
import { Shell } from "./Shell";

function renderShell(initial = "/") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <ModeProvider>
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<div>Home surface</div>} />
            <Route path="history" element={<div>History view content</div>} />
            <Route path="settings" element={<div>Settings view content</div>} />
            <Route path="*" element={<div>Home surface</div>} />
          </Route>
        </Routes>
      </ModeProvider>
    </MemoryRouter>,
  );
}

describe("Application shell", () => {
  it("renders the nav rail, mode indicator, and theme toggle in the chrome", () => {
    renderShell();
    // Nav items are real links (chroma only on active — not asserted here).
    expect(screen.getByRole("link", { name: "New" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "History" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
    // Theme toggle relocated into the shell chrome.
    expect(screen.getByRole("button", { name: /switch to (light|dark)/i })).toBeInTheDocument();
  });

  it("shows Spaces as present-but-dormant (not a link)", () => {
    renderShell();
    expect(screen.queryByRole("link", { name: "Spaces" })).not.toBeInTheDocument();
    const spaces = screen.getByText("Spaces");
    expect(spaces.closest("[aria-disabled='true']")).not.toBeNull();
  });

  it("uses a Search/Build slider as the sole mode control (no separate indicator, no Deep Research at top)", () => {
    renderShell();
    const group = screen.getByRole("radiogroup", { name: "Mode" });
    expect(within(group).getByRole("radio", { name: "search" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(within(group).getByRole("radio", { name: "build" })).toBeDisabled();
    // No separate/duplicate mode indicator; Deep Research is not a top-level mode.
    expect(screen.queryByRole("status", { name: /mode/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/Deep Research/i)).not.toBeInTheDocument();
  });

  it("routes between views via the rail", async () => {
    const user = userEvent.setup();
    renderShell();
    expect(screen.getByText("Home surface")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "History" }));
    expect(screen.getByText("History view content")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(screen.getByText("Settings view content")).toBeInTheDocument();
  });

  it("collapses and expands the rail", async () => {
    const user = userEvent.setup();
    renderShell();
    expect(screen.getByRole("button", { name: "Collapse navigation" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Collapse navigation" }));
    // After collapse, the toggle flips to "Expand navigation" (icon-only rail).
    expect(screen.getByRole("button", { name: "Expand navigation" })).toBeInTheDocument();
  });

  it("opens and closes the mobile drawer without squeezing content", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    // The drawer overlay exposes a backdrop close affordance.
    const close = screen.getByRole("button", { name: "Close navigation" });
    expect(close).toBeInTheDocument();
    await user.click(close);
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
  });
});
