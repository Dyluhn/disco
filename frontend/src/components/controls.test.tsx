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
  it("holds three live modes — Search (active), Build, and Agent — selectable, nothing else", () => {
    render(
      <ModeProvider>
        <ModeSlider />
      </ModeProvider>,
    );
    const group = screen.getByRole("radiogroup", { name: "Mode" });
    expect(group).toBeInTheDocument();
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(3); // exactly search / build / agent
    const search = screen.getByRole("radio", { name: "search" });
    const build = screen.getByRole("radio", { name: "build" });
    const agent = screen.getByRole("radio", { name: "agent" });
    expect(search).toHaveAttribute("aria-checked", "true");
    // Build + Agent are WOKEN — selectable, no "soon" badge.
    expect(build).toBeEnabled();
    expect(agent).toBeEnabled();
    expect(screen.queryByText("soon")).not.toBeInTheDocument();
    // Deep Research is NOT a top-level mode.
    expect(screen.queryByText(/Deep Research/i)).not.toBeInTheDocument();
  });
});

describe("Scope control (one component, mode-driven options)", () => {
  it("under Search offers Standard (default) + Deep Research (live)", async () => {
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
    // Deep Research has been activated — it's now selectable, not dormant.
    expect(deep).not.toHaveAttribute("aria-disabled", "true");
  });
});

describe("Think toggle", () => {
  it("is a switch that toggles and is honestly labeled (reasoning effort, not a fake in-progress)", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<ThinkToggle value={false} onChange={onChange} />);
    const sw = screen.getByRole("switch", { name: /Think/i });
    expect(sw).toHaveAttribute("aria-checked", "false");
    // gap #35: the toggle is genuinely wired (think flag → backend on submit); the
    // title now honestly describes that, replacing the old "backend handling in
    // progress" false-affordance wording.
    expect(sw).toHaveAttribute("title", expect.stringMatching(/reasoning effort/i));
    await user.click(sw);
    expect(onChange).toHaveBeenCalledWith(true);
  });
});
