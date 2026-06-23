/**
 * W-24 (route-resume half) — landing on /agent/:cid syncs the 3-way mode slider
 * to Agent (and /build/:cid → Build, /deep/:cid → Search), so a reload or a
 * deep-link reflects the surface that's actually rendered.
 *
 * The surfaces themselves are stubbed (they own heavy hooks/WS plumbing); this
 * test only asserts the Resume* components' mode-sync effect drives the slider.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// Stub the heavy surfaces — we only care about the slider state on resume.
vi.mock("@/components/AgentSurface", () => ({
  AgentSurface: () => <div data-testid="agent-surface-stub" />,
}));
vi.mock("@/components/BuildSurface", () => ({
  BuildSurface: () => <div data-testid="build-surface-stub" />,
}));
vi.mock("@/components/research/DeepResearchSurface", () => ({
  DeepResearchSurface: () => <div data-testid="deep-surface-stub" />,
}));

import App from "@/App";

afterEach(() => {
  vi.restoreAllMocks();
  window.history.pushState({}, "", "/");
});

async function expectModeChecked(mode: string) {
  await waitFor(() =>
    expect(screen.getByRole("radio", { name: mode })).toHaveAttribute(
      "aria-checked",
      "true",
    ),
  );
}

describe("W-24 — mode slider syncs on route resume", () => {
  it("/agent/:cid → the slider reflects Agent", async () => {
    window.history.pushState({}, "", "/agent/cid_test");
    render(<App />);
    expect(screen.getByTestId("agent-surface-stub")).toBeInTheDocument();
    await expectModeChecked("agent");
  });

  it("/build/:cid → the slider reflects Build", async () => {
    window.history.pushState({}, "", "/build/cid_test");
    render(<App />);
    await expectModeChecked("build");
  });

  it("/deep/:cid → the slider reflects Search", async () => {
    window.history.pushState({}, "", "/deep/cid_test");
    render(<App />);
    await expectModeChecked("search");
  });
});
