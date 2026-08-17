/**
 * BP-12 — AgentStatusBar Resume button: shows for PAUSED and for
 * IDLE-with-unfinished-plan (when onResume is provided by the parent).
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AgentStatusBar } from "@/components/build/AgentStatusBar";
import type { IsolationInfo } from "@/types/agent";

const ISO: IsolationInfo = {
  tier: "local",
  label: "Local container",
  adversarialSafe: false,
};

describe("AgentStatusBar — Resume button (BP-12)", () => {
  it("shows Resume for PAUSED when onResume is provided", async () => {
    const onResume = vi.fn();
    const user = userEvent.setup();
    render(
      <AgentStatusBar
        status="PAUSED"
        isolation={ISO}
        onKill={() => {}}
        onResume={onResume}
      />,
    );
    const btn = screen.getByRole("button", { name: /resume the agent/i });
    await user.click(btn);
    expect(onResume).toHaveBeenCalledTimes(1);
  });

  it("shows Resume for IDLE when onResume is provided (interrupted-with-unfinished-plan)", async () => {
    const onResume = vi.fn();
    const user = userEvent.setup();
    render(
      <AgentStatusBar
        status="IDLE"
        isolation={ISO}
        onKill={() => {}}
        onResume={onResume}
      />,
    );
    const btn = screen.getByRole("button", { name: /resume the agent/i });
    await user.click(btn);
    expect(onResume).toHaveBeenCalledTimes(1);
  });

  it("does NOT show Resume for IDLE when onResume is not provided (fresh conversation)", () => {
    render(
      <AgentStatusBar status="IDLE" isolation={ISO} onKill={() => {}} />,
    );
    expect(
      screen.queryByRole("button", { name: /resume the agent/i }),
    ).not.toBeInTheDocument();
  });

  it("does NOT show Resume for RUNNING even when onResume is provided", () => {
    render(
      <AgentStatusBar
        status="RUNNING"
        isolation={ISO}
        onKill={() => {}}
        onResume={() => {}}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /resume the agent/i }),
    ).not.toBeInTheDocument();
  });

  it("does NOT show Resume for FINISHED even when onResume is provided", () => {
    render(
      <AgentStatusBar
        status="FINISHED"
        isolation={ISO}
        onKill={() => {}}
        onResume={() => {}}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /resume the agent/i }),
    ).not.toBeInTheDocument();
  });

  it("does not show Stop while PAUSED (Resume replaces it)", () => {
    render(
      <AgentStatusBar
        status="PAUSED"
        isolation={ISO}
        onKill={() => {}}
        onStop={() => {}}
        onResume={() => {}}
      />,
    );
    expect(
      screen.queryByRole("button", { name: /stop the agent gracefully/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /resume the agent/i }),
    ).toBeInTheDocument();
  });
});
