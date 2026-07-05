/**
 * W-24 (route-resume half) — landing on /agent/:cid syncs the 3-way mode slider
 * to Agent (and /build/:cid → Build, /deep/:cid → Search), so a reload or a
 * deep-link reflects the surface that's actually rendered.
 *
 * The surfaces themselves are stubbed (they own heavy hooks/WS plumbing); this
 * test only asserts the Resume* components' mode-sync effect drives the slider.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  markAllModesFresh,
  markConversationKilled,
  resetSessionResumeStateForTests,
} from "@/lib/sessionResume";

const mocks = vi.hoisted(() => ({
  listConversations: vi.fn(),
}));

vi.mock("@/api/conversations", () => ({
  listConversations: mocks.listConversations,
}));

// Stub the heavy surfaces — we only care about the slider state on resume.
vi.mock("@/components/AgentSurface", () => ({
  AgentSurface: ({ resumeCid }: { resumeCid?: string | null }) => (
    <div data-testid="agent-surface-stub" data-cid={resumeCid ?? ""} />
  ),
}));
vi.mock("@/components/BuildSurface", () => ({
  BuildSurface: ({ resumeCid }: { resumeCid?: string | null }) => (
    <div data-testid="build-surface-stub" data-cid={resumeCid ?? ""} />
  ),
}));
vi.mock("@/components/ResearchSurface", () => ({
  ResearchSurface: () => <div data-testid="research-surface-stub" />,
}));
vi.mock("@/components/research/DeepResearchSurface", () => ({
  DeepResearchSurface: () => <div data-testid="deep-surface-stub" />,
}));

import App from "@/App";

afterEach(() => {
  vi.restoreAllMocks();
  mocks.listConversations.mockReset();
  resetSessionResumeStateForTests();
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

describe("A2 — fresh sessions and resume targets", () => {
  it("New preserves the active mode, then a mode chip lands on that mode's splash", async () => {
    const user = userEvent.setup();
    mocks.listConversations.mockResolvedValue([
      {
        id: "build_active",
        owner_id: "me",
        title: "Old build",
        created_at: "2026-07-05T12:00:00Z",
        surface: "build",
        status: "RUNNING",
      },
    ]);
    window.history.pushState({}, "", "/agent/agent_old");
    render(<App />);
    await expectModeChecked("agent");

    await user.click(screen.getByRole("link", { name: "New" }));
    await waitFor(() => {
      expect(screen.getByTestId("agent-surface-stub")).toHaveAttribute("data-cid", "");
    });
    expect(window.location.pathname).toBe("/");

    await user.click(screen.getByRole("radio", { name: "build" }));
    await waitFor(() => {
      expect(screen.getByTestId("build-surface-stub")).toHaveAttribute("data-cid", "");
    });
    expect(window.location.pathname).toBe("/");
    expect(mocks.listConversations).not.toHaveBeenCalled();
  });

  it("opening a project clears that mode's fresh flag, so its chip resumes active work", async () => {
    const user = userEvent.setup();
    markAllModesFresh();
    mocks.listConversations.mockResolvedValue([
      {
        id: "build_active",
        owner_id: "me",
        title: "Active build",
        created_at: "2026-07-05T12:00:00Z",
        surface: "build",
        status: "RUNNING",
      },
    ]);
    window.history.pushState({}, "", "/build/build_history");
    render(<App />);
    await expectModeChecked("build");

    await user.click(screen.getByRole("radio", { name: "search" }));
    await waitFor(() => expect(screen.getByTestId("research-surface-stub")).toBeInTheDocument());

    await user.click(screen.getByRole("radio", { name: "build" }));
    await waitFor(() => {
      expect(screen.getByTestId("build-surface-stub")).toHaveAttribute(
        "data-cid",
        "build_active",
      );
    });
    expect(window.location.pathname).toBe("/build/build_active");
  });

  it("does not resume a just-killed conversation id from the mode chip", async () => {
    const user = userEvent.setup();
    markConversationKilled("build_active");
    mocks.listConversations.mockResolvedValue([
      {
        id: "build_active",
        owner_id: "me",
        title: "Killed build",
        created_at: "2026-07-05T12:00:00Z",
        surface: "build",
        status: "RUNNING",
      },
    ]);
    window.history.pushState({}, "", "/");
    render(<App />);
    await expectModeChecked("search");

    await user.click(screen.getByRole("radio", { name: "build" }));
    await waitFor(() => {
      expect(screen.getByTestId("build-surface-stub")).toHaveAttribute("data-cid", "");
    });
    expect(window.location.pathname).toBe("/");
  });
});
