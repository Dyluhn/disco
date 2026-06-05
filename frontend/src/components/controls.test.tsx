import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import { ModeSlider } from "@/shell/ModeSlider";
import { ModelLeaderPill } from "./ModelLeaderPill";
import { ScopeControl } from "./ScopeControl";
import { ThinkToggle } from "./ThinkToggle";

function withQuery(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("Model leader pill (model-only)", () => {
  it("shows the settings default on the pill face, cost-legible, with no mode options", async () => {
    const user = userEvent.setup();
    withQuery(<ModelLeaderPill value={null} onChange={() => {}} />);
    await waitFor(() => expect(screen.getByText(/Driver Local/i)).toBeInTheDocument());
    expect(screen.getByText(/Free/i)).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /choose the model that leads this conversation/i }),
    );
    // The pill is model-only — no mode options leaked into it.
    expect(screen.queryByText(/^Search$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Deep Research/i)).not.toBeInTheDocument();
  });

  it("opens a cost-legible picker grouped local vs overflow and selects a model", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    withQuery(<ModelLeaderPill value={null} onChange={onChange} />);
    await waitFor(() => expect(screen.getByText(/Driver Local/i)).toBeInTheDocument());
    await user.click(
      screen.getByRole("button", { name: /choose the model that leads this conversation/i }),
    );
    expect(screen.getByText(/Local — free/i)).toBeInTheDocument();
    expect(screen.getByText(/Overflow — paid/i)).toBeInTheDocument();
    expect(screen.getByText("$3 / $15 / Mtok")).toBeInTheDocument();
    await user.click(screen.getByText(/Driver Overflow — claude/i));
    expect(onChange).toHaveBeenCalledWith("driver-overflow");
  });
});

describe("Mode slider (the sole mode control)", () => {
  it("holds exactly Search (active) and Build (live, selectable), nothing else", () => {
    render(
      <ModeProvider>
        <ModeSlider />
      </ModeProvider>,
    );
    const group = screen.getByRole("radiogroup", { name: "Mode" });
    expect(group).toBeInTheDocument();
    const search = screen.getByRole("radio", { name: "search" });
    const build = screen.getByRole("radio", { name: "build" });
    expect(search).toHaveAttribute("aria-checked", "true");
    // Build is WOKEN — selectable, no "soon" badge.
    expect(build).toBeEnabled();
    expect(screen.queryByText("soon")).not.toBeInTheDocument();
    // Deep Research is NOT a top-level mode.
    expect(screen.queryByText(/Deep Research/i)).not.toBeInTheDocument();
  });
});

describe("Scope control (one component, mode-driven options)", () => {
  it("under Search offers Standard (default) + Deep Research (dormant)", async () => {
    const user = userEvent.setup();
    render(
      <ModeProvider>
        <ScopeControl value="standard" onChange={() => {}} />
      </ModeProvider>,
    );
    // Trigger reflects the default scope for the active mode.
    await user.click(screen.getByRole("button", { name: /Scope: Standard/i }));
    expect(screen.getByRole("menuitem", { name: /Standard/i })).toBeInTheDocument();
    const deep = screen.getByRole("menuitem", { name: /Deep Research/i });
    expect(deep).toBeInTheDocument();
    expect(deep).toHaveAttribute("aria-disabled", "true");
  });
});

describe("Think toggle", () => {
  it("is a switch that toggles and is honestly labeled as in-progress", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ThinkToggle value={false} onChange={onChange} />);
    const sw = screen.getByRole("switch", { name: /Think/i });
    expect(sw).toHaveAttribute("aria-checked", "false");
    expect(sw).toHaveAttribute("title", expect.stringMatching(/in progress/i));
    await user.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
  });
});
